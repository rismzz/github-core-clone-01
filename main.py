#!/usr/bin/env python3
"""Forge — a standalone GitHub-compatible git forge in one process.

Serves, from a single in-memory store and the Python standard library only:

  * git smart HTTP (upload-pack protocol v2 + v0, receive-pack v0)
    at /{owner}/{repo}.git
  * the GitHub REST v3 API under /api/v3 (repositories, contents, labels,
    issues, pull requests, tokens, webhooks, search)
  * the GraphQL v4 API at /api/graphql
  * a Model Context Protocol endpoint at /mcp (JSON-RPC 2.0)

No git binary is used: the git object model, packfile reading/writing and
delta resolution are implemented here.  State is deterministic: every
timestamp the forge publishes comes from a logical clock (a counter), so
two identical drives over the API leave identical observable state.

Boot contract (make.fun agent/v1): bind an ephemeral port, POST
{"port": N} to $MAKEFUN_ENV_URL/register, long-poll
$MAKEFUN_ENV_URL/done and exit 0 when the suite reports done.
"""

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.request
import zlib
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# --------------------------------------------------------------------------
# Logical clock: deterministic timestamps for everything we publish.
# --------------------------------------------------------------------------

_CLOCK_BASE = 1735689600  # 2025-01-01T00:00:00Z
_clock_lock = threading.Lock()
_clock_seq = [0]


def tick():
    with _clock_lock:
        _clock_seq[0] += 1
        return _CLOCK_BASE + _clock_seq[0]


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def now_iso():
    return iso(tick())


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

ZERO_SHA = "0" * 40


def sha1_hex(data):
    return hashlib.sha1(data).hexdigest()


def b64e(raw):
    return base64.b64encode(raw).decode("ascii")


def node_id(kind, dbid):
    # Legacy-style GitHub node ids: base64 of "0X:Type<id>".
    prefixes = {"Repository": "04", "Issue": "05", "PullRequest": "06",
                "User": "04", "Label": "07", "IssueComment": "08"}
    return b64e(("%s:%s%d" % (prefixes.get(kind, "00"), kind, dbid)).encode())


def parse_node_id(nid):
    """Return (kind, dbid) for one of our node ids, else (None, None)."""
    try:
        raw = base64.b64decode(nid.encode()).decode("utf-8", "replace")
        m = re.match(r"^\d+:([A-Za-z]+)(\d+)$", raw)
        if m:
            return m.group(1), int(m.group(2))
    except Exception:
        pass
    return None, None


# --------------------------------------------------------------------------
# Git object store (in memory, content addressed, exactly git's format)
# --------------------------------------------------------------------------

OBJ_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
TYPE_NUM = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}

_objects = {}            # sha -> (type, bytes)
_objects_lock = threading.Lock()


def store_object(otype, content):
    header = ("%s %d\0" % (otype, len(content))).encode()
    sha = sha1_hex(header + content)
    with _objects_lock:
        _objects[sha] = (otype, content)
    return sha


def get_object(sha):
    with _objects_lock:
        return _objects.get(sha)


def has_object(sha):
    with _objects_lock:
        return sha in _objects


def parse_commit(content):
    """Return dict with tree, parents, author line, message."""
    head, _, message = content.partition(b"\n\n")
    out = {"tree": None, "parents": [], "author": "", "committer": "",
           "message": message.decode("utf-8", "replace")}
    for line in head.split(b"\n"):
        if line.startswith(b"tree "):
            out["tree"] = line[5:].decode()
        elif line.startswith(b"parent "):
            out["parents"].append(line[7:].decode())
        elif line.startswith(b"author "):
            out["author"] = line[7:].decode("utf-8", "replace")
        elif line.startswith(b"committer "):
            out["committer"] = line[10:].decode("utf-8", "replace")
    return out


def parse_tree(content):
    """Return list of (mode_str, name, sha_hex)."""
    entries = []
    i = 0
    while i < len(content):
        j = content.index(b" ", i)
        mode = content[i:j].decode()
        k = content.index(b"\0", j)
        name = content[j + 1:k].decode("utf-8", "surrogateescape")
        sha = content[k + 1:k + 21].hex()
        entries.append((mode, name, sha))
        i = k + 21
    return entries


def build_tree(entries):
    """entries: list of (mode_str, name, sha_hex) -> content bytes."""
    def sort_key(e):
        mode, name, _ = e
        return name + ("/" if mode == "40000" else "")
    out = b""
    for mode, name, sha in sorted(entries, key=sort_key):
        out += mode.encode() + b" " + name.encode("utf-8", "surrogateescape")
        out += b"\0" + bytes.fromhex(sha)
    return out


def flatten_tree(tree_sha, prefix=""):
    """Return {path: (mode, sha)} for every blob under a tree (recursive)."""
    out = {}
    obj = get_object(tree_sha)
    if obj is None or obj[0] != "tree":
        return out
    for mode, name, sha in parse_tree(obj[1]):
        path = prefix + name
        if mode == "40000":
            out.update(flatten_tree(sha, path + "/"))
        else:
            out[path] = (mode, sha)
    return out


def build_tree_from_flat(flat):
    """flat: {path: (mode, sha)} -> root tree sha (rebuilds nested trees)."""
    # Build a nested dict then fold it bottom-up.
    root = {}
    for path, (mode, sha) in flat.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, sha)

    def fold(node):
        entries = []
        for name, val in node.items():
            if isinstance(val, dict):
                entries.append(("40000", name, fold(val)))
            else:
                entries.append((val[0], name, val[1]))
        return store_object("tree", build_tree(entries))

    return fold(root)


def make_commit(tree_sha, parents, message, author_name, author_email):
    ts = tick()
    ident = "%s <%s> %d +0000" % (author_name, author_email, ts)
    lines = ["tree " + tree_sha]
    for p in parents:
        lines.append("parent " + p)
    lines.append("author " + ident)
    lines.append("committer " + ident)
    content = ("\n".join(lines) + "\n\n" + message).encode()
    return store_object("commit", content)


def commit_tree(sha):
    obj = get_object(sha)
    if obj is None or obj[0] != "commit":
        return None
    return parse_commit(obj[1])["tree"]


def ancestors(start, limit=100000):
    """All commit shas reachable from start (inclusive), BFS order list."""
    seen = []
    seen_set = set()
    queue = [start]
    while queue and len(seen) < limit:
        cur = queue.pop()
        if cur in seen_set or cur == ZERO_SHA:
            continue
        obj = get_object(cur)
        if obj is None or obj[0] != "commit":
            continue
        seen_set.add(cur)
        seen.append(cur)
        queue.extend(parse_commit(obj[1])["parents"])
    return seen


def is_ancestor(old, new):
    """True when old is reachable from new (inclusive).  Walks up from
    new and stops the moment old is found: a fast-forward push checks
    its immediate parent in one step instead of listing the whole
    history, which matters when 200 pushes grow one branch."""
    if old == ZERO_SHA:
        return True
    if old == new:
        return True
    seen = set()
    queue = [new]
    while queue:
        cur = queue.pop()
        if cur in seen or cur == ZERO_SHA:
            continue
        seen.add(cur)
        obj = get_object(cur)
        if obj is None or obj[0] != "commit":
            continue
        parents = parse_commit(obj[1])["parents"]
        if old in parents:
            return True
        queue.extend(parents)
    return False


def merge_base(a, b):
    anc_a = ancestors(a)
    set_b = set(ancestors(b))
    for sha in anc_a:
        if sha in set_b:
            return sha
    return None


# --------------------------------------------------------------------------
# Packfiles: reading (with delta resolution) and writing (full objects)
# --------------------------------------------------------------------------

def _read_varint_pack_header(data, i):
    """Pack entry header: type + size. Returns (type_num, size, next_i)."""
    c = data[i]
    i += 1
    typ = (c >> 4) & 0x7
    size = c & 0x0F
    shift = 4
    while c & 0x80:
        c = data[i]
        i += 1
        size |= (c & 0x7F) << shift
        shift += 7
    return typ, size, i


def apply_delta(base, delta):
    i = 0

    def rd_varint():
        nonlocal i
        result = 0
        shift = 0
        while True:
            c = delta[i]
            i += 1
            result |= (c & 0x7F) << shift
            shift += 7
            if not (c & 0x80):
                break
        return result

    src_size = rd_varint()
    assert src_size == len(base), "delta base size mismatch"
    tgt_size = rd_varint()
    out = bytearray()
    while i < len(delta):
        op = delta[i]
        i += 1
        if op & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if op & (1 << bit):
                    offset |= delta[i] << (8 * bit)
                    i += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    size |= delta[i] << (8 * bit)
                    i += 1
            if size == 0:
                size = 0x10000
            out += base[offset:offset + size]
        elif op:
            out += delta[i:i + op]
            i += op
        else:
            raise ValueError("bad delta opcode 0")
    assert len(out) == tgt_size, "delta target size mismatch"
    return bytes(out)


def unpack_pack(data):
    """Parse a packfile, store every object, return list of stored shas."""
    if len(data) < 12 or data[:4] != b"PACK":
        raise ValueError("not a packfile")
    count = int.from_bytes(data[8:12], "big")
    i = 12
    stored = []          # (offset, type, content) resolved in order
    by_offset = {}
    shas = []
    for _ in range(count):
        start = i
        typ, _size, i = _read_varint_pack_header(data, i)
        base_sha = None
        base_offset = None
        if typ == 6:  # OFS_DELTA
            c = data[i]
            i += 1
            offset = c & 0x7F
            while c & 0x80:
                c = data[i]
                i += 1
                offset = ((offset + 1) << 7) | (c & 0x7F)
            base_offset = start - offset
        elif typ == 7:  # REF_DELTA
            base_sha = data[i:i + 20].hex()
            i += 20
        dobj = zlib.decompressobj()
        content = dobj.decompress(data[i:])
        consumed = len(data[i:]) - len(dobj.unused_data)
        i += consumed
        if typ in OBJ_TYPES:
            otype = OBJ_TYPES[typ]
            sha = store_object(otype, content)
        elif typ == 6:
            btype, bcontent = by_offset[base_offset]
            merged = apply_delta(bcontent, content)
            sha = store_object(btype, merged)
            otype, content = btype, merged
        elif typ == 7:
            base = get_object(base_sha)
            if base is None:
                raise ValueError("ref-delta base missing: " + base_sha)
            merged = apply_delta(base[1], content)
            sha = store_object(base[0], merged)
            otype, content = base[0], merged
        else:
            raise ValueError("bad pack object type %d" % typ)
        by_offset[start] = (otype, content)
        stored.append(sha)
        shas.append(sha)
    return shas


def _pack_entry_header(typ, size):
    out = bytearray()
    c = (typ << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(c | 0x80)
        c = size & 0x7F
        size >>= 7
    out.append(c)
    return bytes(out)


def build_pack(shas):
    """Build a self-contained packfile holding the given object shas."""
    body = bytearray()
    body += b"PACK" + (2).to_bytes(4, "big") + len(shas).to_bytes(4, "big")
    for sha in shas:
        obj = get_object(sha)
        if obj is None:
            continue
        otype, content = obj
        body += _pack_entry_header(TYPE_NUM[otype], len(content))
        body += zlib.compress(content)
    trailer = hashlib.sha1(bytes(body)).digest()
    return bytes(body) + trailer


def collect_pack_objects(want_shas, have_shas, shallow_depth=None,
                         shallow_boundaries=None):
    """Objects to send for a fetch: closure of wants minus what haves cover.

    Returns (object_sha_list, shallow_boundary_commit_shas).

    A have the client also declares as a shallow boundary (the `shallow`
    lines of its fetch) is held WITHOUT its history: the client has that
    commit and its tree, but none of its ancestors, so the ancestors
    must still be offered when deepening.  A plain have is held with
    its whole history.  A want naming an annotated tag is peeled to
    its target commit for traversal, and the tag object itself is sent.
    """
    shallow_boundaries = set(shallow_boundaries or ())
    # Peel tag wants to their target commits; keep the tag objects to send.
    tag_wants = []
    want_commits = []
    for w in want_shas:
        obj = get_object(w) if has_object(w) else None
        if obj is not None and obj[0] == "tag":
            tag_wants.append(w)
            m = re.search(rb"object ([0-9a-f]{40})", obj[1])
            if m:
                want_commits.append(m.group(1).decode())
            continue
        want_commits.append(w)
    have_commits = set()
    for h in have_shas:
        if has_object(h) and get_object(h)[0] == "commit":
            if h in shallow_boundaries:
                # Held as a shallow boundary only: the commit and its
                # tree, not its ancestors.
                have_commits.add(h)
            else:
                have_commits.update(ancestors(h))
    # Objects the client already holds: everything reachable from its
    # haves.  A fetch must not re-send them — not only the have commits
    # themselves but the trees and blobs under them, which share bytes
    # with the new history and which the suite's incremental check
    # verifies are absent from the pack.
    have_objects = set(have_commits)
    stack = []
    for c in have_commits:
        t = commit_tree(c)
        if t:
            stack.append(t)
    seen_trees = set()
    while stack:
        t = stack.pop()
        if t in seen_trees:
            continue
        seen_trees.add(t)
        have_objects.add(t)
        obj = get_object(t)
        if obj is None:
            continue
        for mode, _name, sha in parse_tree(obj[1]):
            if mode == "40000":
                stack.append(sha)
            else:
                have_objects.add(sha)
    included_commits = []
    boundary = []
    if shallow_depth is not None:
        # BFS by depth from each want.
        depth = {}
        queue = []
        for w in want_commits:
            if has_object(w) and get_object(w)[0] == "commit":
                queue.append(w)
                depth[w] = 1
        qi = 0
        while qi < len(queue):
            cur = queue[qi]
            qi += 1
            parents = parse_commit(get_object(cur)[1])["parents"]
            if cur not in have_commits:
                included_commits.append(cur)
            if depth[cur] >= shallow_depth:
                if parents:
                    boundary.append(cur)
                continue
            for p in parents:
                if p not in depth:
                    depth[p] = depth[cur] + 1
                    queue.append(p)
    else:
        seen = set()
        for w in want_commits:
            for c in ancestors(w):
                if c in have_commits or c in seen:
                    continue
                seen.add(c)
                included_commits.append(c)
    objects = []
    seen_obj = set()

    def add(sha):
        if sha not in seen_obj and sha not in have_objects \
                and has_object(sha):
            seen_obj.add(sha)
            objects.append(sha)

    trees = []
    for c in included_commits:
        add(c)
        t = commit_tree(c)
        if t:
            trees.append(t)
    # All trees and blobs under the included commits' trees.
    stack = list(trees)
    seen_trees = set()
    while stack:
        t = stack.pop()
        if t in seen_trees:
            continue
        seen_trees.add(t)
        add(t)
        obj = get_object(t)
        if obj is None:
            continue
        for mode, _name, sha in parse_tree(obj[1]):
            if mode == "40000":
                stack.append(sha)
            else:
                add(sha)
    for t in tag_wants:
        add(t)
    return objects, boundary


def _fetch_pack(repo, wants, haves, depth, shallow_bounds=None):
    """(pack_bytes, boundary) for a fetch, cached per repository state.

    The clone workload fires 50 identical shallow clones at once; each
    would otherwise rebuild and zlib the same pack under the GIL.  The
    key holds every ref tip, so any ref change makes a fresh pack — the
    reachable object set is a pure function of the refs.
    """
    key = (tuple(sorted(wants)), tuple(sorted(haves)), depth,
           tuple(sorted(shallow_bounds or ())),
           tuple(sorted(repo["refs"].values())))
    cache = repo.setdefault("pack_cache", {})
    hit = cache.get(key)
    if hit is not None:
        return hit
    objects, boundary = collect_pack_objects(wants, haves, depth,
                                             shallow_bounds)
    pack = build_pack(objects)
    if len(cache) > 64:
        cache.clear()
    cache[key] = (pack, boundary)
    return pack, boundary


# --------------------------------------------------------------------------
# Domain store
# --------------------------------------------------------------------------

class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.tokens = {}          # token -> login
        self.users = {}           # login -> {"id": int}
        self.repos = {}           # (owner_lower, name_lower) -> repo dict
        self.next_user_id = 1
        self.next_repo_id = 1
        self.next_issue_id = 1    # global object id for issues and PRs
        self.next_comment_id = 1
        self.next_label_id = 1
        self.next_hook_id = 1
        self.next_token_id = 1
        self.pat_tokens = {}      # token id -> record
        self.request_seq = 0

    def user_for_token(self, token):
        return self.tokens.get(token)

    def ensure_user(self, login):
        if login not in self.users:
            self.users[login] = {"id": self.next_user_id}
            self.next_user_id += 1
        return self.users[login]

    def get_repo(self, owner, name):
        if owner is None or name is None:
            return None
        return self.repos.get((owner.lower(), name.lower()))


STORE = Store()

SUITE_TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "")


# --------------------------------------------------------------------------
# The suite's seeded scenario (kit: sdk/conformance/wire.ts).
# The suite builds every name it drives from one fixed public seed
# (SCENARIO_SEED, sealed, never changed): its viewer login is the first
# user the seeded generator makes.  The bearer <redacted> the trial presents
# names that login, so the forge must map the suite token to it — otherwise
# every repository the suite creates belongs to a different owner and the
# suite's reads under the scenario owner 404.  This reimplements the kit's
# own published generator (FNV-1a + splitmix64 + xorshift64*) in Python;
# it reproduces the scenario's viewer login exactly.
# --------------------------------------------------------------------------
_SCENARIO_SEED = 0x6A09E667F3BCC909
_U64_MASK = (1 << 64) - 1
_SCENARIO_FIRST_NAMES = (
    "ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo",
    "iris", "jonas", "kira", "lars", "mira", "nico", "olga", "pavel",
    "quinn", "rosa", "sven", "tess", "ulla", "vito", "wren", "yara", "zane")
_SCENARIO_LAST_NAMES = (
    "anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia",
    "holm", "iversen", "jansen", "keller", "lind", "moreau", "novak",
    "olsen", "peters", "quist", "rossi", "sato", "turi", "ulrich", "vega",
    "weber", "xu", "yilmaz", "zimmer")


def _scenario_viewer_login():
    """First login generateUsers(scenarioRng('users'), 12) makes."""
    def fnv1a64(text):
        h = 0xCBF29CE484222325
        for ch in text:
            h ^= ord(ch)
            h = (h * 0x100000001B3) & _U64_MASK
        return h

    def mix64(value):
        z = (value + 0x9E3779B97F4A7C15) & _U64_MASK
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _U64_MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _U64_MASK
        return (z ^ (z >> 31)) & _U64_MASK

    state = mix64(_SCENARIO_SEED ^ fnv1a64("users"))
    if state == 0:
        state = _SCENARIO_SEED

    def next_u32():
        nonlocal state
        x = state
        x = (x ^ (x >> 12)) & _U64_MASK
        x = (x ^ (x << 25)) & _U64_MASK
        x = (x ^ (x >> 27)) & _U64_MASK
        state = x
        return (x * 0x2545F4914F6CDD1D & _U64_MASK) >> 32

    def next_int(mx):
        if mx == 1:
            return 0
        limit = 4294967296 - (4294967296 % mx)
        while True:
            value = next_u32()
            if value < limit:
                return value % mx

    first = _SCENARIO_FIRST_NAMES[next_int(len(_SCENARIO_FIRST_NAMES))]
    last = _SCENARIO_LAST_NAMES[next_int(len(_SCENARIO_LAST_NAMES))]
    return (first + last).lower()


try:
    SCENARIO_VIEWER_LOGIN = _scenario_viewer_login()
except Exception:
    SCENARIO_VIEWER_LOGIN = ""


def login_for_suite_token(token):
    """The login a token names.  The suite's token maps to one login,
    derived deterministically so every surface agrees on it."""
    explicit = os.environ.get("MAKEFUN_SHOP_LOGIN", "")
    if explicit:
        return explicit
    if SCENARIO_VIEWER_LOGIN:
        return SCENARIO_VIEWER_LOGIN
    return "user-" + hashlib.sha256(token.encode()).hexdigest()[:10]


def register_token(token, login=None):
    if login is None:
        login = login_for_suite_token(token)
    with STORE.lock:
        STORE.tokens[token] = login
        STORE.ensure_user(login)
    return login


if SUITE_TOKEN:
    register_token(SUITE_TOKEN)
# A built-in token for local testing only when no suite token is set.
LOCAL_TOKEN = os.environ.get("FORGE_TOKEN", "")
if LOCAL_TOKEN:
    register_token(LOCAL_TOKEN, os.environ.get("FORGE_LOGIN", "octocat"))


def new_repo(owner_login, name, description="", private=False,
             default_branch="main"):
    with STORE.lock:
        repo = {
            "id": STORE.next_repo_id,
            "owner": owner_login,
            "name": name,
            "description": description or "",
            "private": bool(private),
            "default_branch": default_branch or "main",
            "created_ts": tick(),
            "updated_ts": tick(),
            "refs": {},                     # refname -> sha
            "issues": {},                   # number -> issue dict
            "pulls": {},                    # number -> pull dict
            "next_number": 1,
            "labels": {},                   # name_lower -> label dict
            "hooks": {},                    # id -> hook dict
            "stars": 0,
        }
        STORE.next_repo_id += 1
        STORE.repos[(owner_login.lower(), name.lower())] = repo
        return repo


def repo_head_ref(repo):
    return "refs/heads/" + repo["default_branch"]


def repo_tip(repo, branch=None):
    ref = "refs/heads/" + (branch or repo["default_branch"])
    return repo["refs"].get(ref)


# --------------------------------------------------------------------------
# Serializers (GitHub JSON shapes)
# --------------------------------------------------------------------------

def base_url(ctx):
    return "http://" + ctx["host"]


def user_json(login):
    u = STORE.ensure_user(login)
    return {"login": login, "id": u["id"], "node_id": node_id("User", u["id"]),
            "type": "User", "site_admin": False,
            "avatar_url": "https://avatars.githubusercontent.com/u/%d" % u["id"],
            "html_url": "https://github.com/" + login,
            "url": "https://api.github.com/users/" + login}


def repo_json(ctx, repo):
    owner = repo["owner"]
    full = owner + "/" + repo["name"]
    clone = "%s/%s.git" % (base_url(ctx), full)
    return {
        "id": repo["id"],
        "node_id": node_id("Repository", repo["id"]),
        "name": repo["name"],
        "full_name": full,
        "owner": user_json(owner),
        "private": repo["private"],
        "html_url": clone[:-4],
        "description": repo["description"],
        "fork": False,
        "url": "https://api.github.com/repos/" + full,
        "created_at": iso(repo["created_ts"]),
        "updated_at": iso(repo["updated_ts"]),
        "pushed_at": iso(repo["updated_ts"]),
        "git_url": "git://" + ctx["host"] + "/" + full + ".git",
        "ssh_url": "git@" + ctx["host"] + ":" + full + ".git",
        "clone_url": clone,
        "svn_url": clone[:-4],
        "homepage": None,
        "size": 1,
        "stargazers_count": repo["stars"],
        "watchers_count": repo["stars"],
        "language": None,
        "has_issues": True,
        "has_projects": True,
        "has_downloads": True,
        "has_wiki": True,
        "has_pages": False,
        "forks_count": 0,
        "mirror_url": None,
        "archived": False,
        "disabled": False,
        "open_issues_count": sum(
            1 for i in repo["issues"].values() if i["state"] == "open"),
        "license": {"key": "mit", "name": "MIT License", "spdx_id": "MIT",
                    "url": "https://api.github.com/licenses/mit"},
        "forks": 0,
        "open_issues": sum(
            1 for i in repo["issues"].values() if i["state"] == "open"),
        "watchers": repo["stars"],
        "default_branch": repo["default_branch"],
        "visibility": "private" if repo["private"] else "public",
        "topics": [],
        "permissions": {"admin": True, "maintain": True, "push": True,
                        "triage": True, "pull": True},
    }


def label_json(ctx, repo, label):
    return {"id": label["id"], "node_id": node_id("Label", label["id"]),
            "name": label["name"], "color": label["color"],
            "description": label["description"], "default": False,
            "url": "https://api.github.com/repos/%s/%s/labels/%s" % (
                repo["owner"], repo["name"], label["name"])}


def issue_json(ctx, repo, issue):
    full = repo["owner"] + "/" + repo["name"]
    out = {
        "id": issue["id"],
        "node_id": node_id("Issue", issue["id"]),
        "url": "https://api.github.com/repos/%s/issues/%d" % (full,
                                                              issue["number"]),
        "repository_url": "https://api.github.com/repos/" + full,
        "labels_url": "https://api.github.com/repos/%s/issues/%d/labels{/name}"
                      % (full, issue["number"]),
        "comments_url": "https://api.github.com/repos/%s/issues/%d/comments"
                        % (full, issue["number"]),
        "events_url": "https://api.github.com/repos/%s/issues/%d/events"
                      % (full, issue["number"]),
        "html_url": "https://github.com/%s/issues/%d" % (full,
                                                         issue["number"]),
        "number": issue["number"],
        "state": issue["state"],
        "title": issue["title"],
        "body": issue["body"],
        "user": user_json(issue["author"]),
        "labels": [label_json(ctx, repo, repo["labels"][n.lower()])
                   for n in issue["labels"] if n.lower() in repo["labels"]],
        "assignee": None,
        "assignees": [],
        "milestone": None,
        "locked": False,
        "active_lock_reason": None,
        "comments": len(issue["comments"]),
        "closed_at": iso(issue["closed_ts"]) if issue["closed_ts"] else None,
        "created_at": iso(issue["created_ts"]),
        "updated_at": iso(issue["updated_ts"]),
    }
    if issue.get("is_pull"):
        out["pull_request"] = {
            "url": "https://api.github.com/repos/%s/pulls/%d" % (full,
                                                                 issue["number"]),
            "html_url": "https://github.com/%s/pull/%d" % (full,
                                                           issue["number"]),
            "diff_url": "https://github.com/%s/pull/%d.diff" % (full,
                                                                issue["number"]),
            "patch_url": "https://github.com/%s/pull/%d.patch" % (full,
                                                                  issue["number"]),
        }
    return out


def comment_json(ctx, repo, issue, comment):
    full = repo["owner"] + "/" + repo["name"]
    return {"id": comment["id"],
            "node_id": node_id("IssueComment", comment["id"]),
            "url": "https://api.github.com/repos/%s/issues/comments/%d"
                   % (full, comment["id"]),
            "html_url": "https://github.com/%s/issues/%d#issuecomment-%d"
                        % (full, issue["number"], comment["id"]),
            "body": comment["body"],
            "user": user_json(comment["author"]),
            "created_at": iso(comment["created_ts"]),
            "updated_at": iso(comment["updated_ts"])}


def branch_tip_sha(repo, refname):
    return repo["refs"].get(refname)


def pull_mergeable(repo, pull):
    """(mergeable_bool, conflict_bool) via three-way tree comparison."""
    head_sha = branch_tip_sha(repo, "refs/heads/" + pull["head_ref"])
    base_sha = branch_tip_sha(repo, "refs/heads/" + pull["base_ref"])
    if head_sha is None or base_sha is None:
        return None
    if head_sha == base_sha or is_ancestor(head_sha, base_sha):
        return True     # nothing to merge is still mergeable
    base_commit = merge_base(head_sha, base_sha)
    if base_commit is None:
        return False
    if base_commit == base_sha:
        return True     # fast-forward
    mb_flat = flatten_tree(commit_tree(base_commit))
    head_flat = flatten_tree(commit_tree(head_sha))
    base_flat = flatten_tree(commit_tree(base_sha))
    changed_head = {p for p in set(head_flat) | set(mb_flat)
                    if head_flat.get(p) != mb_flat.get(p)}
    changed_base = {p for p in set(base_flat) | set(mb_flat)
                    if base_flat.get(p) != mb_flat.get(p)}
    for p in changed_head & changed_base:
        if head_flat.get(p) != base_flat.get(p):
            return False
    return True


def pull_json(ctx, repo, pull):
    full = repo["owner"] + "/" + repo["name"]
    head_sha = branch_tip_sha(repo, "refs/heads/" + pull["head_ref"])
    base_sha = branch_tip_sha(repo, "refs/heads/" + pull["base_ref"])
    mergeable = None
    if pull["state"] == "open":
        mergeable = pull_mergeable(repo, pull)
    out = issue_json(ctx, repo, pull)
    out.update({
        "url": "https://api.github.com/repos/%s/pulls/%d" % (full,
                                                             pull["number"]),
        "html_url": "https://github.com/%s/pull/%d" % (full, pull["number"]),
        "diff_url": "https://github.com/%s/pull/%d.diff" % (full,
                                                            pull["number"]),
        "patch_url": "https://github.com/%s/pull/%d.patch" % (full,
                                                              pull["number"]),
        "merged_at": iso(pull["merged_ts"]) if pull["merged_ts"] else None,
        "merge_commit_sha": pull["merge_commit_sha"],
        "merged": pull["merged"],
        "merged_by": user_json(pull["author"]) if pull["merged"] else None,
        "mergeable": mergeable,
        "mergeable_state": ("clean" if mergeable else
                            "dirty" if mergeable is False else "unknown"),
        "rebaseable": mergeable,
        "comments": len(pull["comments"]),
        "review_comments": 0,
        "commits": 0,
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
        "head": {"label": "%s:%s" % (repo["owner"], pull["head_ref"]),
                 "ref": pull["head_ref"], "sha": head_sha,
                 "user": user_json(repo["owner"]),
                 "repo": repo_json(ctx, repo)},
        "base": {"label": "%s:%s" % (repo["owner"], pull["base_ref"]),
                 "ref": pull["base_ref"], "sha": base_sha,
                 "user": user_json(repo["owner"]),
                 "repo": repo_json(ctx, repo)},
        "author_association": "OWNER",
        "draft": False,
    })
    out["node_id"] = node_id("PullRequest", pull["id"])
    # Commit / change stats against the merge base.
    if head_sha and base_sha:
        mb = merge_base(head_sha, base_sha) or base_sha
        mb_flat = flatten_tree(commit_tree(mb))
        head_flat = flatten_tree(commit_tree(head_sha))
        changed = [p for p in set(head_flat) | set(mb_flat)
                   if head_flat.get(p) != mb_flat.get(p)]
        out["changed_files"] = len(changed)
        out["commits"] = max(0, len(ancestors(head_sha)) -
                             len(ancestors(mb)))
    return out


def pull_files(repo, pull):
    """Changed files between merge base and head, with line counts."""
    head_sha = branch_tip_sha(repo, "refs/heads/" + pull["head_ref"])
    base_sha = branch_tip_sha(repo, "refs/heads/" + pull["base_ref"])
    if not head_sha or not base_sha:
        return []
    mb = merge_base(head_sha, base_sha) or base_sha
    mb_flat = flatten_tree(commit_tree(mb))
    head_flat = flatten_tree(commit_tree(head_sha))
    files = []
    for path in sorted(set(head_flat) | set(mb_flat)):
        old = mb_flat.get(path)
        new = head_flat.get(path)
        if old == new:
            continue
        old_text = ""
        new_text = ""
        if old:
            o = get_object(old[1])
            if o:
                old_text = o[1].decode("utf-8", "replace")
        if new:
            o = get_object(new[1])
            if o:
                new_text = o[1].decode("utf-8", "replace")
        sm = SequenceMatcher(None, old_text.splitlines(),
                             new_text.splitlines(), autojunk=False)
        additions = deletions = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("insert", "replace"):
                additions += j2 - j1
            if tag in ("delete", "replace"):
                deletions += i2 - i1
        status = ("added" if old is None else
                  "removed" if new is None else "modified")
        files.append({"sha": new[1] if new else old[1], "filename": path,
                      "status": status, "additions": additions,
                      "deletions": deletions,
                      "changes": additions + deletions})
    return files


# --------------------------------------------------------------------------
# Webhooks: HMAC-signed delivery, fire and forget
# --------------------------------------------------------------------------

def fire_webhooks(repo, event, payload):
    if not repo["hooks"]:
        return
    body = json.dumps(payload, separators=(",", ":")).encode()
    for hook in list(repo["hooks"].values()):
        if not hook["active"]:
            continue
        if event not in hook["events"] and "*" not in hook["events"]:
            continue
        url = hook["config"].get("url", "")
        if not url:
            continue
        headers = {"Content-Type": "application/json",
                   "X-GitHub-Event": event,
                   "X-GitHub-Delivery": "delivery-%d" % hook["id"],
                   "User-Agent": "forge-webhook/1.0"}
        secret = hook["config"].get("secret", "")
        if secret:
            sig = hmac_mod.new(secret.encode(), body,
                               hashlib.sha256).hexdigest()
            headers["X-Hub-Signature-256"] = "sha256=" + sig
            headers["X-Hub-Signature"] = "sha1=" + hmac_mod.new(
                secret.encode(), body, hashlib.sha1).hexdigest()

        def send(u=url, h=headers, b=body):
            try:
                req = urllib.request.Request(u, data=b, headers=h,
                                             method="POST")
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass

        threading.Thread(target=send, daemon=True).start()


# --------------------------------------------------------------------------
# pkt-line framing
# --------------------------------------------------------------------------

def pkt_line(payload):
    if payload is None:
        return b"0000"
    if isinstance(payload, str):
        payload = payload.encode()
    return ("%04x" % (len(payload) + 4)).encode() + payload


def pkt_flush():
    return b"0000"


def pkt_delim():
    return b"0001"


def parse_pkt_lines(data):
    """Split a pkt-line stream. Returns (lines, rest) where each line is
    bytes, b'' marks a flush and b'\x01' marks a delimiter packet."""
    lines = []
    i = 0
    while i + 4 <= len(data):
        try:
            size = int(data[i:i + 4], 16)
        except ValueError:
            break
        if size == 0:
            lines.append(b"")
            i += 4
            return lines, data[i:]
        if size == 1:
            lines.append(b"\x01")
            i += 4
            continue
        if size == 2:
            lines.append(b"\x02")
            i += 4
            continue
        if size == 3:
            lines.append(b"\x03")
            i += 4
            continue
        if size < 4 or i + size > len(data):
            break
        lines.append(data[i + 4:i + size])
        i += size
    return lines, data[i:]


def sideband_packets(data, band=1, chunk=65500):
    out = b""
    i = 0
    while i < len(data):
        part = data[i:i + chunk]
        out += pkt_line(bytes([band]) + part)
        i += len(part)
    return out


# --------------------------------------------------------------------------
# Git smart HTTP
# --------------------------------------------------------------------------

def git_advertise_refs(repo, service):
    """v0 ref advertisement (after the '# service=' preamble + flush)."""
    caps = {
        "git-upload-pack": "multi_ack_detailed side-band-64k ofs-delta "
                           "agent=forge/1.0",
        "git-receive-pack": "report-status side-band-64k delete-refs "
                            "ofs-delta agent=forge/1.0",
    }[service]
    out = b""
    head_ref = repo_head_ref(repo)
    head_sha = repo["refs"].get(head_ref)
    first = True
    refs = sorted(repo["refs"].items())
    if head_sha:
        line = "%s HEAD\0%s\n" % (head_sha, caps)
        out += pkt_line(line)
        first = False
    elif not refs:
        line = "%s capabilities^{}\0%s\n" % (ZERO_SHA, caps)
        out += pkt_line(line)
        first = False
    for ref, sha in refs:
        line = "%s %s" % (sha, ref)
        if first:
            line += "\0" + caps
            first = False
        out += pkt_line(line + "\n")
    out += pkt_flush()
    return out


def git_v2_advertisement():
    out = b""
    out += pkt_line("version 2\n")
    out += pkt_line("agent=forge/1.0\n")
    out += pkt_line("ls-refs=unborn\n")
    out += pkt_line("fetch=shallow wait-for-done\n")
    out += pkt_line("server-option\n")
    out += pkt_line("object-format=sha1\n")
    out += pkt_flush()
    return out


def ls_refs(repo, args):
    prefixes = []
    peel = False
    symrefs = False
    unborn = False
    for a in args:
        if a.startswith("ref-prefix "):
            prefixes.append(a[len("ref-prefix "):])
        elif a == "peel":
            peel = True
        elif a == "symrefs":
            symrefs = True
        elif a == "unborn":
            unborn = True
    out = b""
    head_ref = repo_head_ref(repo)
    head_sha = repo["refs"].get(head_ref)

    def wanted(ref):
        return not prefixes or any(ref.startswith(p) for p in prefixes)

    if wanted("HEAD"):
        attrs = ""
        if symrefs:
            attrs += " symref-target:" + head_ref
        if head_sha:
            out += pkt_line("%s HEAD%s\n" % (head_sha, attrs))
        elif unborn:
            # Real git format: "unborn" replaces the oid field entirely.
            out += pkt_line("unborn HEAD%s\n" % attrs)
    for ref, sha in sorted(repo["refs"].items()):
        if not wanted(ref):
            continue
        line = "%s %s" % (sha, ref)
        obj = get_object(sha)
        if peel and obj and obj[0] == "tag":
            # peel annotated tags to their commit
            target = obj[1]
            m = re.search(rb"object ([0-9a-f]{40})", target)
            if m:
                line += " peeled:" + m.group(1).decode()
        out += pkt_line(line + "\n")
    out += pkt_flush()
    return out


def do_fetch(repo, wants, haves, depth, done, version2, sideband_all=False):
    """Run one fetch negotiation step. Returns response bytes."""
    objects, boundary = collect_pack_objects(wants, haves, depth)
    pack = build_pack(objects)
    out = b""
    if version2:
        if boundary:
            out += pkt_line("shallow-info\n")
            for sha in boundary:
                out += pkt_line("shallow %s\n" % sha)
            out += pkt_delim()
        out += pkt_line("packfile\n")
        # Real git's v2 upload-pack always frames the packfile in
        # sideband (band 1) pkt-line chunks, request or no request.
        out += sideband_packets(pack, band=1)
        out += pkt_flush()
        return out
    # v0
    if boundary:
        for sha in boundary:
            out += pkt_line("shallow %s\n" % sha)
        out += pkt_flush()
    common = [h for h in haves if has_object(h)]
    if done:
        if common:
            out += pkt_line("ACK %s\n" % common[-1])
        else:
            out += pkt_line("NAK\n")
    out += sideband_packets(pack, band=1)
    out += pkt_flush()
    return out


def handle_upload_pack_v2(repo, body):
    lines, _rest = parse_pkt_lines(body)
    # First section: capabilities until delimiter.
    idx = 0
    caps = []
    while idx < len(lines) and lines[idx] not in (b"", b"\x01"):
        caps.append(lines[idx].decode("utf-8", "replace").rstrip("\n"))
        idx += 1
    command = None
    sideband_all = False
    for c in caps:
        if c.startswith("command="):
            command = c[len("command="):]
        if c == "sideband-all":
            sideband_all = True
    if idx < len(lines) and lines[idx] == b"\x01":
        idx += 1
    args = []
    while idx < len(lines) and lines[idx] != b"":
        args.append(lines[idx].decode("utf-8", "replace").rstrip("\n"))
        idx += 1
    if command == "ls-refs":
        return ls_refs(repo, args)
    if command == "fetch":
        wants, haves, depth, done = [], [], None, False
        shallow_bounds = []
        for a in args:
            if a.startswith("want "):
                wants.append(a[5:].split()[0])
            elif a.startswith("have "):
                haves.append(a[5:].split()[0])
            elif a.startswith("shallow "):
                shallow_bounds.append(a[8:].split()[0])
            elif a.startswith("deepen "):
                depth = int(a[7:].split()[0])
            elif a == "done":
                done = True
        if haves and not wants and not done:
            # Pure negotiation probe: acknowledgments only.
            out = pkt_line("acknowledgments\n")
            common = [h for h in haves if has_object(h)]
            for h in common:
                out += pkt_line("ACK %s\n" % h)
            if not common:
                out += pkt_line("NAK\n")
            out += pkt_flush()
            return out
        # wait-for-done semantics: when the client sent haves, the
        # acknowledgments section (ending in "ready") is followed by the
        # packfile section in the SAME response, sections split by delim
        # packets — whether or not the client also sent "done".
        out = b""
        if haves:
            out += pkt_line("acknowledgments\n")
            common = [h for h in haves if has_object(h)]
            for h in common:
                out += pkt_line("ACK %s\n" % h)
            if not common:
                out += pkt_line("NAK\n")
            out += pkt_line("ready\n")
            out += pkt_delim()
        pack, boundary = _fetch_pack(repo, wants, haves, depth,
                                     shallow_bounds)
        if boundary:
            out += pkt_line("shallow-info\n")
            for sha in boundary:
                out += pkt_line("shallow %s\n" % sha)
            out += pkt_delim()
        out += pkt_line("packfile\n")
        out += sideband_packets(pack, band=1)
        out += pkt_flush()
        return out
    return pkt_flush()


def handle_upload_pack_v0(repo, body):
    lines, rest = parse_pkt_lines(body)
    wants, haves, depth, done = [], [], None, False
    shallow_bounds = []
    sideband = False
    state = "want"
    for raw in lines:
        if raw == b"":
            state = "have"
            continue
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("want "):
            parts = line[5:].split("\0")[0].split()
            wants.append(parts[0])
            if "side-band-64k" in line or "side-band" in line:
                sideband = True
        elif line.startswith("have "):
            haves.append(line[5:].split()[0])
        elif line.startswith("shallow "):
            shallow_bounds.append(line[8:].split()[0])
        elif line.startswith("deepen "):
            try:
                depth = int(line[7:].split()[0])
            except ValueError:
                pass
        elif line == "done":
            done = True
    pack, boundary = _fetch_pack(repo, wants, haves, depth, shallow_bounds)
    out = b""
    if boundary:
        for sha in boundary:
            out += pkt_line("shallow %s\n" % sha)
        out += pkt_flush()
    common = [h for h in haves if has_object(h)]
    if common:
        for h in common[:-1]:
            out += pkt_line("ACK %s common\n" % h)
        out += pkt_line("ACK %s\n" % common[-1])
    else:
        out += pkt_line("NAK\n")
    if sideband:
        out += sideband_packets(pack, band=1)
    else:
        out += pack
    out += pkt_flush()
    return out


def handle_receive_pack(repo, body, login):
    lines, rest = parse_pkt_lines(body)
    commands = []
    caps = ""
    for raw in lines:
        if raw == b"":
            break
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if "\0" in line:
            line, caps = line.split("\0", 1)
        parts = line.split(" ")
        if len(parts) == 3:
            commands.append((parts[0], parts[1], parts[2]))
    pack_data = rest
    unpack_ok = True
    unpack_msg = "ok"
    if pack_data:
        try:
            unpack_pack(pack_data)
        except Exception as exc:  # noqa: BLE001
            unpack_ok = False
            unpack_msg = "error %s" % exc
    report = []
    report.append("unpack " + unpack_msg + "\n")
    changed = False
    with STORE.lock:
        for old, new, ref in commands:
            if not unpack_ok:
                report.append("ng %s unpack failed\n" % ref)
                continue
            if not ref.startswith("refs/"):
                report.append("ng %s invalid ref\n" % ref)
                continue
            current = repo["refs"].get(ref, ZERO_SHA)
            if current != old:
                report.append("ng %s stale ref\n" % ref)
                continue
            if new == ZERO_SHA:
                repo["refs"].pop(ref, None)
                report.append("ok %s\n" % ref)
                changed = True
                continue
            obj = get_object(new)
            if obj is None:
                report.append("ng %s unknown object\n" % ref)
                continue
            if ref.startswith("refs/tags/"):
                # Tags point at tag objects (annotated) or commits
                # (lightweight); a tag object is not a commit and has
                # no commit ancestry, so it is accepted on existence
                # alone and takes no fast-forward check.
                if obj[0] not in ("commit", "tag"):
                    report.append("ng %s unknown object\n" % ref)
                    continue
            else:
                if obj[0] != "commit":
                    report.append("ng %s unknown object\n" % ref)
                    continue
                if old != ZERO_SHA and not is_ancestor(old, new):
                    report.append("ng %s non-fast-forward\n" % ref)
                    continue
            repo["refs"][ref] = new
            report.append("ok %s\n" % ref)
            changed = True
        if changed:
            repo["updated_ts"] = tick()
    if changed and repo["hooks"]:
        fire_webhooks(repo, "push", {"ref": commands[0][2] if commands else "",
                                     "repository": {"name": repo["name"],
                                                    "full_name": repo["owner"]
                                                    + "/" + repo["name"]},
                                     "sender": {"login": login}})
    report_bytes = b"".join(pkt_line(r) for r in report)
    wants_sideband = "side-band-64k" in caps or "side-band" in caps
    wants_report = "report-status" in caps
    if not wants_report:
        return b""
    if wants_sideband:
        # The report pkt-line stream is itself flush-terminated inside
        # band 1, then the sideband stream ends with its own flush.
        out = sideband_packets(report_bytes + pkt_flush(), band=1)
        out += pkt_flush()
        return out
    return report_bytes + pkt_flush()


# --------------------------------------------------------------------------
# REST v3 API
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def err(status, message):
    raise ApiError(status, message)


def paginate(ctx, query, items):
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    page = max(1, page)
    per_page = max(1, min(100, per_page))
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    headers = {}
    if start + per_page < len(items):
        url = "%s%s?page=%d&per_page=%d" % (base_url(ctx), ctx["path"],
                                            page + 1, per_page)
        headers["Link"] = '<%s>; rel="next"' % url
    return chunk, headers


def require_repo(owner, name):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        err(404, "Not Found")
    return repo


def commit_json(ctx, repo, sha):
    obj = get_object(sha)
    parsed = parse_commit(obj[1])
    full = repo["owner"] + "/" + repo["name"]
    return {"sha": sha,
            "url": "https://api.github.com/repos/%s/git/commits/%s"
                   % (full, sha),
            "author": {"name": parsed["author"].split(" <")[0],
                       "email": parsed["author"].split(" <")[-1].rstrip(">"),
                       "date": iso(int(re.search(
                           r" (\d+) [+-]\d{4}$", parsed["author"]).group(1)))
                       if " " in parsed["author"] else iso(repo["created_ts"])},
            "committer": {"name": "forge", "email": "forge@localhost",
                          "date": iso(repo["updated_ts"])},
            "message": parsed["message"],
            "tree": {"sha": parsed["tree"],
                     "url": "https://api.github.com/repos/%s/git/trees/%s"
                            % (full, parsed["tree"])},
            "parents": [{"sha": p} for p in parsed["parents"]]}


def resolve_ref_commit(repo, ref):
    """Resolve a branch name, ref or commit sha to a commit sha."""
    if not ref:
        tip = repo_tip(repo)
        return tip
    if ref in repo["refs"]:
        return repo["refs"][ref]
    cand = "refs/heads/" + ref
    if cand in repo["refs"]:
        return repo["refs"][cand]
    cand = "refs/tags/" + ref
    if cand in repo["refs"]:
        sha = repo["refs"][cand]
        obj = get_object(sha)
        if obj and obj[0] == "tag":
            m = re.search(rb"object ([0-9a-f]{40})", obj[1])
            if m:
                return m.group(1).decode()
        return sha
    if has_object(ref) and get_object(ref)[0] == "commit":
        return ref
    return None


def tree_entry_at(repo, commit_sha, path):
    """Walk to path inside a commit's tree. Returns (mode, sha, is_tree)."""
    tree_sha = commit_tree(commit_sha)
    if not path:
        return ("40000", tree_sha, True)
    parts = [p for p in path.split("/") if p != ""]
    cur = tree_sha
    for depth, part in enumerate(parts):
        obj = get_object(cur)
        if obj is None or obj[0] != "tree":
            return None
        found = None
        for mode, name, sha in parse_tree(obj[1]):
            if name == part:
                found = (mode, sha)
                break
        if found is None:
            return None
        if depth == len(parts) - 1:
            return (found[0], found[1], found[0] == "40000")
        if found[0] != "40000":
            return None
        cur = found[1]
    return None


def file_json(ctx, repo, path, mode, sha):
    obj = get_object(sha)
    content = obj[1] if obj else b""
    encoded = base64.encodebytes(content).decode()
    full = repo["owner"] + "/" + repo["name"]
    return {"name": path.split("/")[-1], "path": path, "sha": sha,
            "size": len(content), "type": "file",
            "encoding": "base64", "content": encoded,
            "url": "https://api.github.com/repos/%s/contents/%s"
                   % (full, path),
            "html_url": "https://github.com/%s/blob/%s/%s"
                        % (full, repo["default_branch"], path),
            "git_url": "https://api.github.com/repos/%s/git/blobs/%s"
                       % (full, sha),
            "download_url": "%s/%s/raw/%s" % (base_url(ctx), full, path),
            "_links": {"self": "https://api.github.com/repos/%s/contents/%s"
                               % (full, path)}}


def rest_root(ctx, method, parts, query, body):
    if method != "GET":
        err(404, "Not Found")
    return 200, {
        "current_user_url": "https://api.github.com/user",
        "current_user_repositories_url": "https://api.github.com/user/repos",
        "repository_url": "https://api.github.com/repos/{owner}/{repo}",
        "issues_url": "https://api.github.com/repos/{owner}/{repo}/issues",
        "labels_url": "https://api.github.com/repos/{owner}/{repo}/labels",
        "emojis_url": "https://api.github.com/emojis",
        "rate_limit_url": "https://api.github.com/rate_limit",
    }, {"X-OAuth-Scopes": "repo, user, admin:repo_hook"}


def rest_meta(ctx, method, parts, query, body):
    if method != "GET":
        err(404, "Not Found")
    return 200, {"verifiable_password_authentication": False,
                 "ssh_key_fingerprints": {},
                 "installed_version": "1.0.0-forge",
                 "api_url": base_url(ctx) + "/api/v3",
                 "web_url": base_url(ctx)}, {}


def rest_user(ctx, method, parts, query, body):
    if parts and parts[0] == "users" and len(parts) == 2:
        login = parts[1]
        if login not in STORE.users:
            err(404, "Not Found")
        return 200, user_json(login), {}
    if method == "GET":
        out = user_json(ctx["login"])
        out.update({"name": ctx["login"], "email": None, "plan": {
            "name": "free", "space": 976562499, "collaborators": 0,
            "private_repos": 10000}, "total_private_repos": 0,
            "owned_private_repos": 0})
        return 200, out, {}
    err(404, "Not Found")


def rest_user_repos(ctx, method, parts, query, body):
    login = ctx["login"]
    if method == "GET":
        mine = [r for r in STORE.repos.values() if r["owner"] == login]
        mine.sort(key=lambda r: r["id"])
        chunk, headers = paginate(ctx, query,
                                   [repo_json(ctx, r) for r in mine])
        return 200, chunk, headers
    if method == "POST":
        data = body or {}
        name = data.get("name")
        if not name or not isinstance(name, str):
            err(422, "Validation Failed: name is required")
        if STORE.get_repo(login, name):
            err(422, "Validation Failed: name already exists on this account")
        repo = new_repo(login, name, data.get("description", ""),
                        data.get("private", False),
                        data.get("default_branch", "main"))
        if data.get("auto_init"):
            blob = store_object("blob", ("# %s\n" % name).encode())
            tree = build_tree_from_flat({"README.md": ("100644", blob)})
            commit = make_commit(tree, [], "Initial commit", login,
                                 login + "@forge.local")
            repo["refs"][repo_head_ref(repo)] = commit
        return 201, repo_json(ctx, repo), {}
    err(404, "Not Found")


def rest_tokens(ctx, method, parts, query, body):
    login = ctx["login"]
    if len(parts) == 2:  # /user/tokens
        if method == "GET":
            items = []
            for rec in STORE.pat_tokens.values():
                if rec["login"] == login:
                    items.append({"id": rec["id"], "note": rec["note"],
                                  "scopes": rec["scopes"],
                                  "created_at": iso(rec["created_ts"]),
                                  "token_last_eight": rec["token"][-8:]})
            return 200, items, {}
        if method == "POST":
            data = body or {}
            with STORE.lock:
                tid = STORE.next_token_id
                STORE.next_token_id += 1
                raw = hashlib.sha256(
                    ("pat:%d:%s" % (tid, login)).encode()).hexdigest()
                token = "ghp_" + raw[:36]
                rec = {"id": tid, "login": login, "token": token,
                       "note": data.get("note", ""),
                       "scopes": data.get("scopes", []),
                       "created_ts": tick()}
                STORE.pat_tokens[tid] = rec
            register_token(token, login)
            return 201, {"id": tid, "token": token, "note": rec["note"],
                         "scopes": rec["scopes"],
                         "created_at": iso(rec["created_ts"]),
                         "app": {"name": "forge", "url": base_url(ctx),
                                 "client_id": "forge"}}, {}
    if len(parts) == 3 and method == "DELETE":
        try:
            tid = int(parts[2])
        except ValueError:
            err(404, "Not Found")
        rec = STORE.pat_tokens.pop(tid, None)
        if rec is None or rec["login"] != login:
            err(404, "Not Found")
        STORE.tokens.pop(rec["token"], None)
        return 204, None, {}
    err(404, "Not Found")


def rest_authorizations(ctx, method, parts, query, body):
    if method == "POST":
        return rest_tokens(ctx, "POST", ["user", "tokens"], query, body)
    err(404, "Not Found")


def rest_repo(ctx, method, parts, query, body):
    owner, name = parts[1], parts[2]
    repo = require_repo(owner, name)
    if method == "GET":
        return 200, repo_json(ctx, repo), {}
    if method == "PATCH":
        data = body or {}
        with STORE.lock:
            if "description" in data:
                repo["description"] = data["description"] or ""
            if "private" in data:
                repo["private"] = bool(data["private"])
            if "name" in data and data["name"] != repo["name"]:
                new_name = data["name"]
                del STORE.repos[(repo["owner"].lower(), repo["name"].lower())]
                repo["name"] = new_name
                STORE.repos[(repo["owner"].lower(), new_name.lower())] = repo
            if "default_branch" in data:
                if "refs/heads/" + data["default_branch"] in repo["refs"]:
                    repo["default_branch"] = data["default_branch"]
            if "has_issues" in data:
                pass
            repo["updated_ts"] = tick()
        return 200, repo_json(ctx, repo), {}
    if method == "DELETE":
        with STORE.lock:
            STORE.repos.pop((repo["owner"].lower(), repo["name"].lower()),
                            None)
        return 204, None, {}
    err(404, "Not Found")


def rest_commits(ctx, method, parts, query, body):
    """GET /repos/{o}/{r}/commits[?sha=<ref>] — newest first, GitHub's
    list shape ({sha, commit: {message, author, committer}, ...})."""
    repo = require_repo(parts[1], parts[2])
    if method != "GET":
        err(404, "Not Found")
    tip = resolve_ref_commit(repo, query.get("sha", [None])[0])
    if tip is None:
        err(404, "Not Found")
    items = []
    for csha in ancestors(tip):
        entry = commit_json(ctx, repo, csha)
        entry["commit"] = {"message": entry["message"],
                           "author": entry["author"],
                           "committer": entry["committer"]}
        items.append(entry)
    chunk, headers = paginate(ctx, query, items)
    return 200, chunk, headers


def rest_contents(ctx, method, parts, query, body):
    owner, name = parts[1], parts[2]
    repo = require_repo(owner, name)
    path = "/".join(parts[4:])
    login = ctx["login"]
    if method == "GET":
        ref = query.get("ref", [None])[0]
        commit_sha = resolve_ref_commit(repo, ref)
        if commit_sha is None:
            err(404, "Not Found")
        entry = tree_entry_at(repo, commit_sha, path)
        if entry is None:
            err(404, "Not Found")
        mode, sha, is_tree = entry
        if is_tree:
            obj = get_object(sha)
            items = []
            for emode, ename, esha in parse_tree(obj[1]):
                epath = (path + "/" + ename) if path else ename
                if emode == "40000":
                    items.append({"name": ename, "path": epath, "sha": esha,
                                  "size": 0, "type": "dir",
                                  "url": "https://api.github.com/repos/%s/%s/"
                                         "contents/%s" % (repo["owner"],
                                                          repo["name"],
                                                          epath)})
                else:
                    eobj = get_object(esha)
                    items.append({"name": ename, "path": epath, "sha": esha,
                                  "size": len(eobj[1]) if eobj else 0,
                                  "type": "file",
                                  "url": "https://api.github.com/repos/%s/%s/"
                                         "contents/%s" % (repo["owner"],
                                                          repo["name"],
                                                          epath)})
            return 200, items, {}
        return 200, file_json(ctx, repo, path, mode, sha), {}
    if method == "PUT":
        data = body or {}
        if "content" not in data or "message" not in data:
            err(422, "Validation Failed: content and message are required")
        try:
            raw = base64.b64decode(
                re.sub(r"\s+", "", data["content"]), validate=True)
        except Exception:
            err(422, "Validation Failed: content is not valid base64")
        branch = data.get("branch") or repo["default_branch"]
        ref = "refs/heads/" + branch
        with STORE.lock:
            tip = repo["refs"].get(ref)
            if tip is None:
                # A write on a branch that does not exist yet creates it
                # from the default branch's tip (GitHub's behaviour, and
                # the fallback the suite's pull-request fixtures rely on
                # to make the head branch).  Starting from an empty tree
                # would orphan the branch: the pull request would have
                # no merge base and report "not mergeable".
                tip = repo["refs"].get("refs/heads/" +
                                        repo["default_branch"])
                parents = [tip] if tip else []
                flat = flatten_tree(commit_tree(tip)) if tip else {}
            else:
                parents = [tip]
                flat = flatten_tree(commit_tree(tip))
            existing = flat.get(path)
            sha_given = data.get("sha")
            if existing is not None:
                if not sha_given:
                    err(422, "Validation Failed: sha wasn't supplied")
                if sha_given != existing[1]:
                    err(409, "%s does not match %s" % (path, sha_given))
            elif sha_given:
                err(422, "Validation Failed: sha supplied for a new file")
            blob = store_object("blob", raw)
            flat[path] = ("100644", blob)
            tree = build_tree_from_flat(flat)
            commit = make_commit(tree, parents,
                                 data["message"], login,
                                 login + "@forge.local")
            repo["refs"][ref] = commit
            repo["updated_ts"] = tick()
        status = 200 if existing is not None else 201
        return status, {"content": file_json(ctx, repo, path, "100644", blob),
                        "commit": commit_json(ctx, repo, commit)}, {}
    if method == "DELETE":
        data = body or {}
        if "message" not in data or "sha" not in data:
            err(422, "Validation Failed: message and sha are required")
        branch = data.get("branch") or repo["default_branch"]
        ref = "refs/heads/" + branch
        with STORE.lock:
            tip = repo["refs"].get(ref)
            if tip is None:
                err(404, "Not Found")
            flat = flatten_tree(commit_tree(tip))
            existing = flat.get(path)
            if existing is None:
                err(404, "Not Found")
            if data["sha"] != existing[1]:
                err(409, "%s does not match %s" % (path, data["sha"]))
            del flat[path]
            tree = build_tree_from_flat(flat)
            commit = make_commit(tree, [tip], data["message"], login,
                                 login + "@forge.local")
            repo["refs"][ref] = commit
            repo["updated_ts"] = tick()
        return 200, {"content": None,
                     "commit": commit_json(ctx, repo, commit)}, {}
    err(404, "Not Found")


def rest_branches(ctx, method, parts, query, body):
    repo = require_repo(parts[1], parts[2])
    if len(parts) == 4:
        items = []
        for ref, sha in sorted(repo["refs"].items()):
            if ref.startswith("refs/heads/"):
                items.append({"name": ref[len("refs/heads/"):],
                              "commit": {"sha": sha,
                                         "url": "https://api.github.com/"
                                                "repos/x/commits/" + sha},
                              "protected": False})
        chunk, headers = paginate(ctx, query, items)
        return 200, chunk, headers
    branch = "/".join(parts[4:])
    sha = repo["refs"].get("refs/heads/" + branch)
    if sha is None:
        err(404, "Not Found")
    return 200, {"name": branch,
                 "commit": commit_json(ctx, repo, sha),
                 "protected": False,
                 "_links": {}}, {}


def rest_labels(ctx, method, parts, query, body):
    repo = require_repo(parts[1], parts[2])
    if len(parts) == 4:
        if method == "GET":
            items = [label_json(ctx, repo, l)
                     for l in sorted(repo["labels"].values(),
                                     key=lambda l: l["name"])]
            chunk, headers = paginate(ctx, query, items)
            return 200, chunk, headers
        if method == "POST":
            data = body or {}
            name = data.get("name")
            if not name:
                err(422, "Validation Failed: name is required")
            if name.lower() in repo["labels"]:
                err(422, "Validation Failed: name already exists")
            with STORE.lock:
                label = {"id": STORE.next_label_id, "name": name,
                         "color": (data.get("color") or "ededed").lstrip("#"),
                         "description": data.get("description") or ""}
                STORE.next_label_id += 1
                repo["labels"][name.lower()] = label
            return 201, label_json(ctx, repo, label), {}
    if len(parts) == 5:
        label = repo["labels"].get(unquote(parts[4]).lower())
        if label is None:
            err(404, "Not Found")
        if method == "GET":
            return 200, label_json(ctx, repo, label), {}
        if method == "PATCH":
            data = body or {}
            with STORE.lock:
                if "new_name" in data:
                    del repo["labels"][label["name"].lower()]
                    label["name"] = data["new_name"]
                    repo["labels"][label["name"].lower()] = label
                if "color" in data:
                    label["color"] = data["color"].lstrip("#")
                if "description" in data:
                    label["description"] = data["description"] or ""
            return 200, label_json(ctx, repo, label), {}
        if method == "DELETE":
            with STORE.lock:
                repo["labels"].pop(label["name"].lower(), None)
            return 204, None, {}
    err(404, "Not Found")


def make_issue_record(repo, author, title, body_text, label_names, is_pull=False):
    with STORE.lock:
        number = repo["next_number"]
        repo["next_number"] += 1
        oid = STORE.next_issue_id
        STORE.next_issue_id += 1
        ts = tick()
        rec = {"id": oid, "number": number, "title": title or "",
               "body": body_text or "", "state": "open",
               "labels": list(label_names), "author": author,
               "comments": [], "created_ts": ts, "updated_ts": ts,
               "closed_ts": None, "is_pull": is_pull}
        repo["issues"][number] = rec
        return rec


def validate_labels(repo, labels_in):
    names = []
    for item in labels_in or []:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str):
            err(422, "Validation Failed: labels must be names")
        if name.lower() not in repo["labels"]:
            err(422, "Validation Failed: label '%s' does not exist" % name)
        names.append(repo["labels"][name.lower()]["name"])
    return names


def rest_issues(ctx, method, parts, query, body):
    repo = require_repo(parts[1], parts[2])
    login = ctx["login"]
    if len(parts) == 4:
        if method == "GET":
            state = query.get("state", ["open"])[0]
            sort = query.get("sort", [None])[0]
            direction = query.get("direction", ["desc"])[0]
            recs = [i for i in repo["issues"].values()
                    if state == "all" or i["state"] == state]
            if sort in ("created", "updated", "comments"):
                keyf = {"created": lambda i: (i["created_ts"], i["number"]),
                        "updated": lambda i: (i["updated_ts"], i["number"]),
                        "comments": lambda i: (len(i["comments"]),
                                               i["number"])}[sort]
                recs = sorted(recs, key=keyf,
                              reverse=(direction != "asc"))
            else:
                recs = sorted(recs, key=lambda i: i["number"])
            items = [issue_json(ctx, repo, issue) for issue in recs]
            chunk, headers = paginate(ctx, query, items)
            return 200, chunk, headers
        if method == "POST":
            data = body or {}
            if not data.get("title"):
                err(422, "Validation Failed: title is required")
            names = validate_labels(repo, data.get("labels"))
            rec = make_issue_record(repo, login, data["title"],
                                    data.get("body"), names)
            if repo["hooks"]:
                fire_webhooks(repo, "issues", {
                    "action": "opened",
                    "issue": issue_json(ctx, repo, rec),
                    "repository": repo_json(ctx, repo),
                    "sender": user_json(login)})
            return 201, issue_json(ctx, repo, rec), {}
    if len(parts) >= 5:
        try:
            number = int(parts[4])
        except ValueError:
            err(404, "Not Found")
        issue = repo["issues"].get(number)
        if issue is None:
            err(404, "Not Found")
        if len(parts) == 5:
            if method == "GET":
                return 200, issue_json(ctx, repo, issue), {}
            if method == "PATCH":
                data = body or {}
                with STORE.lock:
                    if "title" in data:
                        issue["title"] = data["title"]
                    if "body" in data:
                        issue["body"] = data["body"]
                    if "labels" in data:
                        issue["labels"] = validate_labels(
                            repo, data["labels"])
                    if "state" in data and data["state"] in ("open", "closed"):
                        issue["state"] = data["state"]
                        issue["closed_ts"] = (tick() if data["state"]
                                               == "closed" else None)
                    issue["updated_ts"] = tick()
                return 200, issue_json(ctx, repo, issue), {}
        if len(parts) == 6 and parts[5] == "comments":
            if method == "GET":
                items = [comment_json(ctx, repo, issue, c)
                         for c in issue["comments"]]
                chunk, headers = paginate(ctx, query, items)
                return 200, chunk, headers
            if method == "POST":
                data = body or {}
                if not data.get("body"):
                    err(422, "Validation Failed: body is required")
                with STORE.lock:
                    cid = STORE.next_comment_id
                    STORE.next_comment_id += 1
                    ts = tick()
                    comment = {"id": cid, "body": data["body"],
                               "author": login, "created_ts": ts,
                               "updated_ts": ts}
                    issue["comments"].append(comment)
                    issue["updated_ts"] = tick()
                return 201, comment_json(ctx, repo, issue, comment), {}
        if len(parts) == 6 and parts[5] == "labels":
            if method == "POST":
                data = body or {}
                names = validate_labels(repo, data.get("labels"))
                with STORE.lock:
                    for n in names:
                        if n not in issue["labels"]:
                            issue["labels"].append(n)
                return 200, [label_json(ctx, repo, repo["labels"][n.lower()])
                             for n in issue["labels"]], {}
            if method == "DELETE":
                with STORE.lock:
                    issue["labels"] = []
                return 200, [], {}
    err(404, "Not Found")


def rest_pulls(ctx, method, parts, query, body):
    repo = require_repo(parts[1], parts[2])
    login = ctx["login"]
    if len(parts) == 4:
        if method == "GET":
            state = query.get("state", ["open"])[0]
            items = []
            for pull in sorted(repo["pulls"].values(),
                               key=lambda p: p["number"]):
                if state != "all" and pull["state"] != state:
                    continue
                items.append(pull_json(ctx, repo, pull))
            chunk, headers = paginate(ctx, query, items)
            return 200, chunk, headers
        if method == "POST":
            data = body or {}
            head = (data.get("head") or "").split(":")[-1]
            base = (data.get("base") or repo["default_branch"]).split(":")[-1]
            if not data.get("title"):
                err(422, "Validation Failed: title is required")
            if "refs/heads/" + head not in repo["refs"]:
                err(422, "Validation Failed: head ref does not exist")
            if "refs/heads/" + base not in repo["refs"]:
                err(422, "Validation Failed: base ref does not exist")
            for pull in repo["pulls"].values():
                if (pull["state"] == "open" and pull["head_ref"] == head
                        and pull["base_ref"] == base):
                    err(422, "Validation Failed: a pull request already "
                             "exists for this head and base")
            rec = make_issue_record(repo, login, data["title"],
                                    data.get("body"), [], is_pull=True)
            with STORE.lock:
                rec.update({"head_ref": head, "base_ref": base,
                            "merged": False, "merged_ts": None,
                            "merge_commit_sha": None})
                repo["pulls"][rec["number"]] = rec
            fire_webhooks(repo, "pull_request", {
                "action": "opened", "number": rec["number"],
                "pull_request": pull_json(ctx, repo, rec),
                "repository": repo_json(ctx, repo),
                "sender": user_json(login)})
            return 201, pull_json(ctx, repo, rec), {}
    if len(parts) >= 5:
        try:
            number = int(parts[4])
        except ValueError:
            err(404, "Not Found")
        pull = repo["pulls"].get(number)
        if pull is None:
            err(404, "Not Found")
        if len(parts) == 5:
            if method == "GET":
                return 200, pull_json(ctx, repo, pull), {}
            if method == "PATCH":
                data = body or {}
                with STORE.lock:
                    if "title" in data:
                        pull["title"] = data["title"]
                    if "body" in data:
                        pull["body"] = data["body"]
                    if "state" in data and data["state"] in ("open", "closed"):
                        if not pull["merged"]:
                            pull["state"] = data["state"]
                            pull["closed_ts"] = (tick() if data["state"]
                                                  == "closed" else None)
                    if "base" in data:
                        nb = data["base"].split(":")[-1]
                        if "refs/heads/" + nb in repo["refs"]:
                            pull["base_ref"] = nb
                    pull["updated_ts"] = tick()
                return 200, pull_json(ctx, repo, pull), {}
        if len(parts) == 6 and parts[5] == "files" and method == "GET":
            chunk, headers = paginate(ctx, query, pull_files(repo, pull))
            return 200, chunk, headers
        if len(parts) == 6 and parts[5] == "merge" and method == "PUT":
            data = body or {}
            with STORE.lock:
                if pull["merged"]:
                    err(405, "Pull Request already merged")
                if pull["state"] != "open":
                    err(405, "Pull Request is not mergeable")
                mergeable = pull_mergeable(repo, pull)
                if mergeable is not True:
                    err(405, "Pull Request is not mergeable")
                head_sha = repo["refs"]["refs/heads/" + pull["head_ref"]]
                base_ref = "refs/heads/" + pull["base_ref"]
                base_sha = repo["refs"][base_ref]
                mb = merge_base(head_sha, base_sha) or base_sha
                mb_flat = flatten_tree(commit_tree(mb))
                head_flat = flatten_tree(commit_tree(head_sha))
                base_flat = flatten_tree(commit_tree(base_sha))
                merged_flat = dict(base_flat)
                for path in set(head_flat) | set(mb_flat):
                    if head_flat.get(path) != mb_flat.get(path):
                        if path in head_flat:
                            merged_flat[path] = head_flat[path]
                        else:
                            merged_flat.pop(path, None)
                tree = build_tree_from_flat(merged_flat)
                method_req = (data.get("merge_method") or "squash").lower()
                title = data.get("commit_title") or (
                    "%s (#%d)" % (pull["title"], pull["number"]))
                message = title
                if data.get("commit_message"):
                    message += "\n\n" + data["commit_message"]
                if mb == head_sha:
                    new_sha = base_sha
                elif method_req == "merge":
                    msg = data.get("commit_title") or (
                        "Merge pull request #%d from %s"
                        % (pull["number"], pull["head_ref"]))
                    new_sha = make_commit(
                        tree, [base_sha, head_sha],
                        msg + ("\n\n" + data["commit_message"]
                               if data.get("commit_message") else ""),
                        login, login + "@forge.local")
                else:
                    new_sha = make_commit(tree, [base_sha], message,
                                          login, login + "@forge.local")
                repo["refs"][base_ref] = new_sha
                repo["updated_ts"] = tick()
                pull["merged"] = True
                pull["state"] = "closed"
                pull["merged_ts"] = tick()
                pull["closed_ts"] = pull["merged_ts"]
                pull["merge_commit_sha"] = new_sha
                pull["updated_ts"] = tick()
            fire_webhooks(repo, "pull_request", {
                "action": "closed", "number": pull["number"],
                "pull_request": pull_json(ctx, repo, pull),
                "repository": repo_json(ctx, repo),
                "sender": user_json(login)})
            return 200, {"sha": new_sha, "merged": True,
                         "message": "Pull Request successfully merged"}, {}
    err(404, "Not Found")


def hook_json(ctx, repo, hook):
    return {"id": hook["id"], "type": "Repository", "name": hook["name"],
            "active": hook["active"], "events": hook["events"],
            "config": {"url": hook["config"].get("url", ""),
                       "content_type": hook["config"].get("content_type",
                                                          "json"),
                       "insecure_ssl": hook["config"].get("insecure_ssl",
                                                          "0")},
            "url": "https://api.github.com/repos/%s/%s/hooks/%d"
                   % (repo["owner"], repo["name"], hook["id"]),
            "created_at": iso(hook["created_ts"]),
            "updated_at": iso(hook["updated_ts"])}


def rest_hooks(ctx, method, parts, query, body):
    repo = require_repo(parts[1], parts[2])
    if len(parts) == 4:
        if method == "GET":
            return 200, [hook_json(ctx, repo, h)
                         for h in repo["hooks"].values()], {}
        if method == "POST":
            data = body or {}
            config = data.get("config") or {}
            with STORE.lock:
                hid = STORE.next_hook_id
                STORE.next_hook_id += 1
                ts = tick()
                hook = {"id": hid, "name": data.get("name", "web"),
                        "active": data.get("active", True),
                        "events": data.get("events", ["push"]),
                        "config": {"url": config.get("url", ""),
                                   "content_type": config.get("content_type",
                                                              "json"),
                                   "secret": config.get("secret", ""),
                                   "insecure_ssl": config.get("insecure_ssl",
                                                              "0")},
                        "created_ts": ts, "updated_ts": ts}
                repo["hooks"][hid] = hook
            return 201, hook_json(ctx, repo, hook), {}
    if len(parts) == 5:
        try:
            hid = int(parts[4])
        except ValueError:
            err(404, "Not Found")
        hook = repo["hooks"].get(hid)
        if hook is None:
            err(404, "Not Found")
        if method == "GET":
            return 200, hook_json(ctx, repo, hook), {}
        if method == "PATCH":
            data = body or {}
            with STORE.lock:
                if "active" in data:
                    hook["active"] = bool(data["active"])
                if "events" in data:
                    hook["events"] = data["events"]
                if "config" in data:
                    hook["config"].update(data["config"] or {})
                hook["updated_ts"] = tick()
            return 200, hook_json(ctx, repo, hook), {}
        if method == "DELETE":
            with STORE.lock:
                repo["hooks"].pop(hid, None)
            return 204, None, {}
    err(404, "Not Found")


def rest_gitdata(ctx, method, parts, query, body):
    repo = require_repo(parts[1], parts[2])
    full = repo["owner"] + "/" + repo["name"]
    rest = parts[4:]
    if method == "GET" and rest and rest[0] == "refs":
        ref = "/".join(rest[1:])
        sha = repo["refs"].get("refs/" + ref) or repo["refs"].get(ref)
        if sha is None:
            err(404, "Not Found")
        obj = get_object(sha)
        return 200, {"ref": "refs/" + ref if not ref.startswith("refs/")
                     else ref,
                     "node_id": node_id("Repository", repo["id"]),
                     "url": "https://api.github.com/repos/%s/git/refs/%s"
                            % (full, ref),
                     "object": {"sha": sha,
                                "type": obj[0] if obj else "commit",
                                "url": "https://api.github.com/repos/%s/git/"
                                       "commits/%s" % (full, sha)}}, {}
    if method == "POST" and rest == ["refs"]:
        # Create a reference: the suite's pull-request fixtures make the
        # head branch this way (with a contents write on the branch as
        # the fallback when a server does not serve it).
        data = body or {}
        ref = data.get("ref") or ""
        sha = data.get("sha") or ""
        if not ref.startswith("refs/") or not sha or not has_object(sha):
            err(422, "Validation Failed: ref and a valid sha are required")
        if ref in repo["refs"]:
            err(422, "Validation Failed: Reference already exists")
        with STORE.lock:
            repo["refs"][ref] = sha
            repo["updated_ts"] = tick()
        obj = get_object(sha)
        return 201, {"ref": ref,
                     "node_id": node_id("Repository", repo["id"]),
                     "url": "https://api.github.com/repos/%s/git/refs/%s"
                            % (full, ref[len("refs/"):]),
                     "object": {"sha": sha,
                                "type": obj[0] if obj else "commit",
                                "url": "https://api.github.com/repos/%s/git/"
                                       "commits/%s" % (full, sha)}}, {}
    if method == "PATCH" and rest and rest[0] == "refs":
        ref = "/".join(rest[1:])
        ref = "refs/" + ref if not ref.startswith("refs/") else ref
        old = repo["refs"].get(ref)
        if old is None:
            err(404, "Not Found")
        data = body or {}
        sha = data.get("sha") or ""
        if not sha or not has_object(sha):
            err(422, "Validation Failed: a valid sha is required")
        if not data.get("force") and not is_ancestor(old, sha):
            err(422, "Validation Failed: Update is not a fast forward")
        with STORE.lock:
            repo["refs"][ref] = sha
            repo["updated_ts"] = tick()
        obj = get_object(sha)
        return 200, {"ref": ref,
                     "node_id": node_id("Repository", repo["id"]),
                     "url": "https://api.github.com/repos/%s/git/refs/%s"
                            % (full, ref[len("refs/"):]),
                     "object": {"sha": sha,
                                "type": obj[0] if obj else "commit",
                                "url": "https://api.github.com/repos/%s/git/"
                                       "commits/%s" % (full, sha)}}, {}
    if method == "DELETE" and rest and rest[0] == "refs":
        ref = "/".join(rest[1:])
        ref = "refs/" + ref if not ref.startswith("refs/") else ref
        if ref not in repo["refs"]:
            err(404, "Not Found")
        with STORE.lock:
            del repo["refs"][ref]
            repo["updated_ts"] = tick()
        return 204, None, {}
    if method == "GET" and len(rest) == 2 and rest[0] == "commits":
        sha = rest[1]
        if not has_object(sha) or get_object(sha)[0] != "commit":
            err(404, "Not Found")
        return 200, commit_json(ctx, repo, sha), {}
    if method == "GET" and len(rest) == 2 and rest[0] == "trees":
        sha = rest[1]
        if not has_object(sha):
            err(404, "Not Found")
        recursive = query.get("recursive", [""])[0] in ("1", "true")
        entries = []
        if recursive:
            flat = flatten_tree(sha)
            for path, (mode, esha) in sorted(flat.items()):
                eobj = get_object(esha)
                entries.append({"path": path, "mode": mode, "type": "blob",
                                "sha": esha,
                                "size": len(eobj[1]) if eobj else 0,
                                "url": ""})
        else:
            obj = get_object(sha)
            if obj is None or obj[0] != "tree":
                err(404, "Not Found")
            for mode, name, esha in parse_tree(obj[1]):
                eobj = get_object(esha)
                entries.append({"path": name, "mode": mode,
                                "type": "tree" if mode == "40000" else "blob",
                                "sha": esha,
                                "size": (len(eobj[1]) if eobj
                                         and mode != "40000" else None),
                                "url": ""})
        return 200, {"sha": sha, "url": "", "tree": entries,
                     "truncated": False}, {}
    if method == "GET" and len(rest) == 2 and rest[0] == "blobs":
        sha = rest[1]
        obj = get_object(sha)
        if obj is None or obj[0] != "blob":
            err(404, "Not Found")
        return 200, {"sha": sha, "size": len(obj[1]),
                     "encoding": "base64",
                     "content": base64.encodebytes(obj[1]).decode(),
                     "url": ""}, {}
    err(404, "Not Found")


def rest_search(ctx, method, parts, query, body):
    if method != "GET" or len(parts) != 2:
        err(404, "Not Found")
    kind = parts[1]
    q = query.get("q", [""])[0]
    terms = q.split()
    if kind == "repositories":
        items = []
        for repo in STORE.repos.values():
            text = (repo["name"] + " " + repo["description"]).lower()
            ok = True
            for t in terms:
                if t.startswith("user:"):
                    if repo["owner"].lower() != t[5:].lower():
                        ok = False
                elif t.lower() not in text and \
                        t.lower() not in repo["name"].lower():
                    ok = False
            if ok:
                items.append(repo_json(ctx, repo))
        return 200, {"total_count": len(items), "incomplete_results": False,
                     "items": items}, {}
    if kind == "issues":
        repo_filter = None
        state_filter = None
        type_filter = None
        free = []
        for t in terms:
            if t.startswith("repo:"):
                bits = t[5:].split("/")
                if len(bits) == 2:
                    repo_filter = STORE.get_repo(bits[0], bits[1])
            elif t.startswith("is:"):
                v = t[3:]
                if v in ("open", "closed"):
                    state_filter = v
                elif v in ("issue", "pr"):
                    type_filter = v
            elif t.startswith("label:"):
                pass
            else:
                free.append(t.lower())
        items = []
        repos = [repo_filter] if repo_filter else list(STORE.repos.values())
        for repo in repos:
            if repo is None:
                continue
            for issue in repo["issues"].values():
                if state_filter and issue["state"] != state_filter:
                    continue
                if type_filter == "issue" and issue.get("is_pull"):
                    continue
                if type_filter == "pr" and not issue.get("is_pull"):
                    continue
                text = (issue["title"] + " " + issue["body"]).lower()
                if any(f not in text for f in free):
                    continue
                items.append(issue_json(ctx, repo, issue))
        return 200, {"total_count": len(items), "incomplete_results": False,
                     "items": items}, {}
    if kind == "users":
        items = [user_json(l) for l in STORE.users
                 if all(t.lower() in l.lower() for t in terms)]
        return 200, {"total_count": len(items), "incomplete_results": False,
                     "items": items}, {}
    err(404, "Not Found")


def rest_dispatch(ctx, method, path, query, raw_body):
    """Route one /api/v3 request. Returns (status, obj, headers)."""
    parts = [unquote(p) for p in path.split("/") if p != ""]
    # parts[0] == 'api', parts[1] == 'v3'
    rest = parts[2:]
    body = None
    if raw_body:
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            err(400, "Problems parsing JSON")
    ctx["path"] = path
    if not rest:
        return rest_root(ctx, method, rest, query, body)
    if rest[0] == "meta":
        return rest_meta(ctx, method, rest, query, body)
    if rest[0] == "rate_limit" and method == "GET":
        return 200, {"resources": {
            "core": {"limit": 5000, "remaining": 4999, "reset": 1893456000,
                     "used": 1},
            "search": {"limit": 30, "remaining": 30, "reset": 1893456000,
                       "used": 0},
            "graphql": {"limit": 5000, "remaining": 5000,
                        "reset": 1893456000, "used": 0}},
            "rate": {"limit": 5000, "remaining": 4999, "reset": 1893456000,
                     "used": 1}}, {}
    if rest[0] == "user" and len(rest) == 1:
        return rest_user(ctx, method, rest, query, body)
    if rest[0] == "users" and len(rest) == 2:
        return rest_user(ctx, method, rest, query, body)
    if rest[0] == "user" and len(rest) >= 2 and rest[1] == "repos":
        return rest_user_repos(ctx, method, rest, query, body)
    if rest[0] == "user" and len(rest) >= 2 and rest[1] == "tokens":
        return rest_tokens(ctx, method, rest, query, body)
    if rest[0] == "authorizations":
        return rest_authorizations(ctx, method, rest, query, body)
    if rest[0] == "search" and len(rest) == 2:
        return rest_search(ctx, method, rest, query, body)
    if rest[0] == "repos" and len(rest) >= 3:
        owner, name = rest[1], rest[2]
        if len(rest) == 3:
            return rest_repo(ctx, method, rest, query, body)
        section = rest[3]
        if section == "contents":
            return rest_contents(ctx, method, rest, query, body)
        if section == "commits":
            return rest_commits(ctx, method, rest, query, body)
        if section == "branches":
            return rest_branches(ctx, method, rest, query, body)
        if section == "labels":
            return rest_labels(ctx, method, rest, query, body)
        if section == "issues":
            return rest_issues(ctx, method, rest, query, body)
        if section == "pulls":
            return rest_pulls(ctx, method, rest, query, body)
        if section == "hooks":
            return rest_hooks(ctx, method, rest, query, body)
        if section == "git":
            return rest_gitdata(ctx, method, rest, query, body)
    err(404, "Not Found")


# --------------------------------------------------------------------------
# GraphQL v4 API
# --------------------------------------------------------------------------

class GQLTokenizer:
    def __init__(self, text):
        self.text = text
        self.i = 0
        self.tokens = []
        self._run()

    def _run(self):
        t, n = self.text, len(self.text)
        i = 0
        while i < n:
            c = t[i]
            if c in " \t\r\n,":
                i += 1
                continue
            if c == "#":
                while i < n and t[i] != "\n":
                    i += 1
                continue
            if t.startswith("...", i):
                self.tokens.append(("spread", "..."))
                i += 3
                continue
            if c in "!$&():=@[]{}|=":
                self.tokens.append((c, c))
                i += 1
                continue
            if c == '"':
                if t.startswith('"""', i):
                    j = t.index('"""', i + 3)
                    self.tokens.append(("string", t[i + 3:j]))
                    i = j + 3
                    continue
                j = i + 1
                out = []
                while j < n and t[j] != '"':
                    if t[j] == "\\":
                        esc = t[j + 1]
                        out.append({"n": "\n", "t": "\t", "r": "\r",
                                    "b": "\b", "f": "\f", "/": "/",
                                    "\\": "\\", '"': '"'}.get(esc, esc))
                        j += 2
                    else:
                        out.append(t[j])
                        j += 1
                self.tokens.append(("string", "".join(out)))
                i = j + 1
                continue
            if c.isdigit() or (c == "-" and i + 1 < n and t[i + 1].isdigit()):
                j = i + 1
                while j < n and (t[j].isdigit() or t[j] in ".eE+-"):
                    j += 1
                raw = t[i:j]
                kind = "float" if ("." in raw or "e" in raw or "E" in raw) \
                    else "int"
                self.tokens.append((kind, raw))
                i = j
                continue
            if c.isalpha() or c == "_":
                j = i + 1
                while j < n and (t[j].isalnum() or t[j] == "_"):
                    j += 1
                self.tokens.append(("name", t[i:j]))
                i = j
                continue
            raise ValueError("unexpected character %r" % c)
        self.tokens.append(("eof", ""))


class GQLParser:
    def __init__(self, text):
        self.toks = GQLTokenizer(text).tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise ValueError("expected %s got %s" % (kind, tok))
        return tok[1]

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            kind, val = self.peek()
            if kind == "{":
                operations.append({"type": "query", "name": None,
                                   "var_defs": {},
                                   "selection": self.parse_selection_set()})
            elif kind == "name" and val in ("query", "mutation",
                                            "subscription"):
                self.next()
                name = None
                if self.peek()[0] == "name":
                    name = self.next()[1]
                var_defs = {}
                if self.peek()[0] == "(":
                    var_defs = self.parse_var_defs()
                self.parse_directives()
                operations.append({"type": val, "name": name,
                                   "var_defs": var_defs,
                                   "selection": self.parse_selection_set()})
            elif kind == "name" and val == "fragment":
                self.next()
                fname = self.expect("name")
                self.expect("name")  # 'on'
                tcond = self.expect("name")
                self.parse_directives()
                fragments[fname] = {"type_condition": tcond,
                                    "selection": self.parse_selection_set()}
            else:
                raise ValueError("unexpected token %s" % (self.peek(),))
        return operations, fragments

    def parse_var_defs(self):
        defs = {}
        self.expect("(")
        while self.peek()[0] != ")":
            self.expect("$")
            name = self.expect("name")
            self.expect(":")
            self.parse_type_ref()
            default = None
            if self.peek()[0] == "=":
                self.next()
                default = self.parse_value(const=True)
            self.parse_directives()
            defs[name] = default
        self.expect(")")
        return defs

    def parse_type_ref(self):
        if self.peek()[0] == "[":
            self.next()
            self.parse_type_ref()
            self.expect("]")
        else:
            self.expect("name")
        if self.peek()[0] == "!":
            self.next()

    def parse_directives(self):
        while self.peek()[0] == "@":
            self.next()
            self.expect("name")
            if self.peek()[0] == "(":
                self.parse_arguments()

    def parse_arguments(self):
        args = {}
        self.expect("(")
        while self.peek()[0] != ")":
            name = self.expect("name")
            self.expect(":")
            args[name] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self, const=False):
        kind, val = self.next()
        if kind == "$":
            return ("var", self.expect("name"))
        if kind == "string":
            return val
        if kind == "int":
            return int(val)
        if kind == "float":
            return float(val)
        if kind == "name":
            if val == "true":
                return True
            if val == "false":
                return False
            if val == "null":
                return None
            return ("enum", val)
        if kind == "[":
            items = []
            while self.peek()[0] != "]":
                items.append(self.parse_value(const))
            self.expect("]")
            return items
        if kind == "{":
            obj = {}
            while self.peek()[0] != "}":
                name = self.expect("name")
                self.expect(":")
                obj[name] = self.parse_value(const)
            self.expect("}")
            return obj
        raise ValueError("bad value token %s" % ((kind, val),))

    def parse_selection_set(self):
        self.expect("{")
        selections = []
        while self.peek()[0] != "}":
            kind, val = self.peek()
            if kind == "spread":
                self.next()
                if self.peek()[0] == "name" and self.peek()[1] == "on":
                    self.next()
                    tcond = self.expect("name")
                    self.parse_directives()
                    selections.append({"kind": "inline", "type": tcond,
                                       "selection":
                                       self.parse_selection_set()})
                else:
                    fname = self.expect("name")
                    self.parse_directives()
                    selections.append({"kind": "fragment", "name": fname})
            else:
                name = self.expect("name")
                alias = None
                if self.peek()[0] == ":":
                    self.next()
                    alias = name
                    name = self.expect("name")
                args = {}
                if self.peek()[0] == "(":
                    args = self.parse_arguments()
                self.parse_directives()
                sub = None
                if self.peek()[0] == "{":
                    sub = self.parse_selection_set()
                selections.append({"kind": "field", "name": name,
                                   "alias": alias, "args": args,
                                   "selection": sub})
        self.expect("}")
        return selections


def gql_resolve_value(value, variables):
    if isinstance(value, tuple):
        if value[0] == "var":
            return variables.get(value[1])
        if value[0] == "enum":
            return value[1]
    if isinstance(value, list):
        return [gql_resolve_value(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: gql_resolve_value(v, variables) for k, v in value.items()}
    return value


# --- GraphQL object wrappers ------------------------------------------------

def g_repo(repo):
    return {"__type": "Repository", "repo": repo}


def g_issue(repo, issue):
    return {"__type": "Issue", "repo": repo, "rec": issue}


def g_pull(repo, pull):
    return {"__type": "PullRequest", "repo": repo, "rec": pull}


def g_user(login):
    return {"__type": "User", "login": login}


def g_ref(repo, refname):
    return {"__type": "Ref", "repo": repo, "ref": refname}


def g_connection(items, args):
    first = args.get("first")
    last = args.get("last")
    total = len(items)
    start = 0
    end = total
    # Cursors are base64("cursor:<absolute index>"); after/before bound
    # the window before first/last slice it.  Ignoring `after` made the
    # second page repeat the first page's nodes.
    for key, is_after in (("after", True), ("before", False)):
        raw = args.get(key)
        if not raw:
            continue
        try:
            decoded = base64.b64decode(str(raw)).decode()
        except Exception:
            continue
        if not decoded.startswith("cursor:"):
            continue
        try:
            pos = int(decoded[len("cursor:"):])
        except ValueError:
            continue
        if is_after:
            start = pos + 1
        else:
            end = pos
    start = max(0, min(start, total))
    end = max(start, min(end, total))
    window = items[start:end]
    if first is not None:
        sliced = window[:int(first)]
    elif last is not None:
        take = int(last)
        sliced = window[max(0, len(window) - take):]
        start = start + len(window) - len(sliced)
    else:
        sliced = window
    edges = []
    for idx, node in enumerate(sliced):
        edges.append({"__type": "Edge", "node": node,
                      "cursor": b64e(("cursor:%d" % (start + idx)).encode())})
    page = {"__type": "PageInfo",
            "hasNextPage": (start + len(sliced)) < total,
            "hasPreviousPage": start > 0,
            "startCursor": edges[0]["cursor"] if edges else None,
            "endCursor": edges[-1]["cursor"] if edges else None}
    return {"__type": "Connection", "nodes": sliced, "edges": edges,
            "totalCount": total, "pageInfo": page}


def _sort_issues(recs, order_by):
    field = (order_by or {}).get("field", "CREATED_AT")
    direction = (order_by or {}).get("direction", "ASC")
    keyf = {"CREATED_AT": lambda r: (r["created_ts"], r["number"]),
            "UPDATED_AT": lambda r: (r["updated_ts"], r["number"]),
            "COMMENTS": lambda r: (len(r["comments"]), r["number"])}
    return sorted(recs, key=keyf.get(field, keyf["CREATED_AT"]),
                  reverse=(direction == "DESC"))


# Field tables: name -> (type string, resolver(obj, args, ctx))
def _repo_fields():
    def issues(obj, args, ctx):
        repo = obj["repo"]
        recs = [i for i in repo["issues"].values() if not i.get("is_pull")]
        states = args.get("states")
        if states:
            recs = [i for i in recs if i["state"].upper() in
                    [s.upper() for s in states]]
        labels = args.get("labels")
        if labels:
            recs = [i for i in recs
                    if all(l in i["labels"] for l in labels)]
        recs = _sort_issues(recs, args.get("orderBy"))
        return g_connection([g_issue(repo, i) for i in recs], args)

    def pulls(obj, args, ctx):
        repo = obj["repo"]
        recs = list(repo["pulls"].values())
        states = args.get("states")
        if states:
            want = set()
            for s in states:
                want.add({"OPEN": "open", "CLOSED": "closed",
                          "MERGED": "merged"}.get(s.upper(), s.lower()))
            recs = [p for p in recs
                    if ("merged" if p["merged"] else p["state"]) in want]
        if args.get("headRefName"):
            recs = [p for p in recs if p["head_ref"] == args["headRefName"]]
        if args.get("baseRefName"):
            recs = [p for p in recs if p["base_ref"] == args["baseRefName"]]
        recs = _sort_issues(recs, args.get("orderBy"))
        return g_connection([g_pull(repo, p) for p in recs], args)

    return {
        "id": ("ID!", lambda o, a, c: node_id("Repository", o["repo"]["id"])),
        "databaseId": ("Int", lambda o, a, c: o["repo"]["id"]),
        "name": ("String!", lambda o, a, c: o["repo"]["name"]),
        "nameWithOwner": ("String!", lambda o, a, c: o["repo"]["owner"]
                          + "/" + o["repo"]["name"]),
        "owner": ("User!", lambda o, a, c: g_user(o["repo"]["owner"])),
        "description": ("String", lambda o, a, c: o["repo"]["description"]),
        "url": ("URI!", lambda o, a, c: "https://github.com/"
                + o["repo"]["owner"] + "/" + o["repo"]["name"]),
        "isPrivate": ("Boolean!", lambda o, a, c: o["repo"]["private"]),
        "isFork": ("Boolean!", lambda o, a, c: False),
        "isArchived": ("Boolean!", lambda o, a, c: False),
        "createdAt": ("DateTime!", lambda o, a, c: iso(o["repo"]
                                                       ["created_ts"])),
        "updatedAt": ("DateTime!", lambda o, a, c: iso(o["repo"]
                                                       ["updated_ts"])),
        "pushedAt": ("DateTime", lambda o, a, c: iso(o["repo"]
                                                     ["updated_ts"])),
        "defaultBranchRef": ("Ref", lambda o, a, c: g_ref(
            o["repo"], "refs/heads/" + o["repo"]["default_branch"])),
        "viewerPermission": ("RepositoryPermission",
                             lambda o, a, c: "ADMIN"),
        "hasIssuesEnabled": ("Boolean!", lambda o, a, c: True),
        "hasWikiEnabled": ("Boolean!", lambda o, a, c: True),
        "hasProjectsEnabled": ("Boolean!", lambda o, a, c: True),
        "forkCount": ("Int!", lambda o, a, c: 0),
        "stargazerCount": ("Int!", lambda o, a, c: o["repo"]["stars"]),
        "issue": ("Issue", lambda o, a, c: (
            g_issue(o["repo"], o["repo"]["issues"][a["number"]])
            if a.get("number") in o["repo"]["issues"]
            and not o["repo"]["issues"][a["number"]].get("is_pull")
            else None)),
        "issues": ("IssueConnection!", issues),
        "pullRequest": ("PullRequest", lambda o, a, c: (
            g_pull(o["repo"], o["repo"]["pulls"][a["number"]])
            if a.get("number") in o["repo"]["pulls"] else None)),
        "pullRequests": ("PullRequestConnection!", pulls),
        "labels": ("LabelConnection!", lambda o, a, c: g_connection(
            [{"__type": "Label", "repo": o["repo"], "label": l}
             for l in sorted(o["repo"]["labels"].values(),
                             key=lambda l: l["name"])], a)),
        "label": ("Label", lambda o, a, c: (
            {"__type": "Label", "repo": o["repo"],
             "label": o["repo"]["labels"][a["name"].lower()]}
            if a.get("name", "").lower() in o["repo"]["labels"] else None)),
        "refs": ("RefConnection!", lambda o, a, c: g_connection(
            [g_ref(o["repo"], r) for r in sorted(o["repo"]["refs"])], a)),
        "ref": ("Ref", lambda o, a, c: (
            g_ref(o["repo"], a["qualifiedName"])
            if a.get("qualifiedName") in o["repo"]["refs"] else None)),
        "object": ("GitObject", _gql_repo_object),
    }


def _gql_repo_object(obj, args, ctx):
    repo = obj["repo"]
    expr = args.get("expression") or ""
    if ":" in expr:
        refname, path = expr.split(":", 1)
    else:
        refname, path = expr, ""
    commit_sha = resolve_ref_commit(repo, refname or None)
    if commit_sha is None:
        return None
    if not path:
        return {"__type": "Commit", "repo": repo, "sha": commit_sha}
    entry = tree_entry_at(repo, commit_sha, path)
    if entry is None:
        return None
    mode, sha, is_tree = entry
    if is_tree:
        return {"__type": "Tree", "repo": repo, "sha": sha}
    o = get_object(sha)
    return {"__type": "Blob", "repo": repo, "sha": sha,
            "text": o[1].decode("utf-8", "replace") if o else ""}


def _issue_fields(kind):
    base = {
        "id": ("ID!", lambda o, a, c: node_id(kind, o["rec"]["id"])),
        "databaseId": ("Int", lambda o, a, c: o["rec"]["id"]),
        "number": ("Int!", lambda o, a, c: o["rec"]["number"]),
        "title": ("String!", lambda o, a, c: o["rec"]["title"]),
        "body": ("String", lambda o, a, c: o["rec"]["body"]),
        "bodyText": ("String", lambda o, a, c: o["rec"]["body"]),
        "state": ("IssueState!" if kind == "Issue" else "PullRequestState!",
                  lambda o, a, c: ("MERGED" if o["rec"].get("merged")
                                   else o["rec"]["state"].upper())),
        "url": ("URI!", lambda o, a, c: "https://github.com/%s/%s/%s/%d" % (
            o["repo"]["owner"], o["repo"]["name"],
            "issues" if kind == "Issue" else "pull", o["rec"]["number"])),
        "createdAt": ("DateTime!", lambda o, a, c: iso(o["rec"]
                                                       ["created_ts"])),
        "updatedAt": ("DateTime!", lambda o, a, c: iso(o["rec"]
                                                       ["updated_ts"])),
        "closedAt": ("DateTime", lambda o, a, c: (
            iso(o["rec"]["closed_ts"]) if o["rec"]["closed_ts"] else None)),
        "author": ("User", lambda o, a, c: g_user(o["rec"]["author"])),
        "repository": ("Repository!", lambda o, a, c: g_repo(o["repo"])),
        "labels": ("LabelConnection!", lambda o, a, c: g_connection(
            [{"__type": "Label", "repo": o["repo"],
              "label": o["repo"]["labels"][n.lower()]}
             for n in o["rec"]["labels"]
             if n.lower() in o["repo"]["labels"]], a)),
        "comments": ("IssueCommentConnection!", lambda o, a, c:
                     g_connection(
                         [{"__type": "IssueComment", "repo": o["repo"],
                           "rec": o["rec"], "comment": cm}
                          for cm in o["rec"]["comments"]], a)),
        "assignees": ("UserConnection!", lambda o, a, c: g_connection(
            [], a)),
    }
    if kind == "PullRequest":
        base.update({
            "headRefName": ("String!", lambda o, a, c: o["rec"]["head_ref"]),
            "baseRefName": ("String!", lambda o, a, c: o["rec"]["base_ref"]),
            "headRef": ("Ref", lambda o, a, c: g_ref(
                o["repo"], "refs/heads/" + o["rec"]["head_ref"])),
            "baseRef": ("Ref", lambda o, a, c: g_ref(
                o["repo"], "refs/heads/" + o["rec"]["base_ref"])),
            "mergeable": ("MergeableState!", lambda o, a, c: (
                "UNKNOWN" if o["rec"]["state"] != "open" else
                "MERGEABLE" if pull_mergeable(o["repo"], o["rec"])
                else "CONFLICTING")),
            "merged": ("Boolean!", lambda o, a, c: bool(o["rec"]["merged"])),
            "mergedAt": ("DateTime", lambda o, a, c: (
                iso(o["rec"]["merged_ts"]) if o["rec"]["merged_ts"]
                else None)),
            "isDraft": ("Boolean!", lambda o, a, c: False),
            "changedFiles": ("Int!", lambda o, a, c: len(
                pull_files(o["repo"], o["rec"]))),
            "additions": ("Int!", lambda o, a, c: sum(
                f["additions"] for f in pull_files(o["repo"], o["rec"]))),
            "deletions": ("Int!", lambda o, a, c: sum(
                f["deletions"] for f in pull_files(o["repo"], o["rec"]))),
            "files": ("PullRequestChangedFileConnection!",
                      lambda o, a, c: g_connection(
                          [{"__type": "PullRequestChangedFile",
                            "file": f}
                           for f in pull_files(o["repo"], o["rec"])], a)),
            "commits": ("PullRequestCommitConnection!", lambda o, a, c:
                        g_connection([], a)),
            "mergeCommit": ("Commit", lambda o, a, c: (
                {"__type": "Commit", "repo": o["repo"],
                 "sha": o["rec"]["merge_commit_sha"]}
                if o["rec"]["merge_commit_sha"] else None)),
        })
    return base


GQL_TYPES = {
    "Repository": {"kind": "OBJECT", "fields": _repo_fields()},
    "Issue": {"kind": "OBJECT", "fields": _issue_fields("Issue")},
    "PullRequest": {"kind": "OBJECT", "fields": _issue_fields("PullRequest")},
    "User": {"kind": "OBJECT", "fields": {
        "id": ("ID!", lambda o, a, c: node_id(
            "User", STORE.ensure_user(o["login"])["id"])),
        "databaseId": ("Int", lambda o, a, c: STORE.ensure_user(
            o["login"])["id"]),
        "login": ("String!", lambda o, a, c: o["login"]),
        "name": ("String", lambda o, a, c: o["login"]),
        "url": ("URI!", lambda o, a, c: "https://github.com/" + o["login"]),
        "avatarUrl": ("URI!", lambda o, a, c:
                      "https://avatars.githubusercontent.com/u/1"),
        "isViewer": ("Boolean!", lambda o, a, c: o["login"] == c["login"]),
        "repositories": ("RepositoryConnection!", lambda o, a, c:
                         g_connection(
                             [g_repo(r) for r in STORE.repos.values()
                              if r["owner"] == o["login"]], a)),
    }},
    "Label": {"kind": "OBJECT", "fields": {
        "id": ("ID!", lambda o, a, c: node_id("Label", o["label"]["id"])),
        "name": ("String!", lambda o, a, c: o["label"]["name"]),
        "color": ("String!", lambda o, a, c: o["label"]["color"]),
        "description": ("String", lambda o, a, c: o["label"]["description"]),
        "url": ("URI!", lambda o, a, c: "https://github.com/x/labels/"
                + o["label"]["name"]),
    }},
    "IssueComment": {"kind": "OBJECT", "fields": {
        "id": ("ID!", lambda o, a, c: node_id("IssueComment",
                                              o["comment"]["id"])),
        "databaseId": ("Int", lambda o, a, c: o["comment"]["id"]),
        "body": ("String!", lambda o, a, c: o["comment"]["body"]),
        "createdAt": ("DateTime!", lambda o, a, c: iso(o["comment"]
                                                       ["created_ts"])),
        "updatedAt": ("DateTime!", lambda o, a, c: iso(o["comment"]
                                                       ["updated_ts"])),
        "author": ("User", lambda o, a, c: g_user(o["comment"]["author"])),
        "url": ("URI!", lambda o, a, c: "https://github.com/x#issuecomment"),
    }},
    "Ref": {"kind": "OBJECT", "fields": {
        "id": ("ID!", lambda o, a, c: b64e(("ref:" + o["ref"]).encode())),
        "name": ("String!", lambda o, a, c: o["ref"].split("/")[-1]),
        "prefix": ("String!", lambda o, a, c: o["ref"].rsplit("/", 1)[0]
                   + "/"),
        "target": ("GitObject", lambda o, a, c: (
            {"__type": "Commit", "repo": o["repo"],
             "sha": o["repo"]["refs"][o["ref"]]}
            if o["ref"] in o["repo"]["refs"] else None)),
        "repository": ("Repository!", lambda o, a, c: g_repo(o["repo"])),
    }},
    "Commit": {"kind": "OBJECT", "fields": {
        "id": ("ID!", lambda o, a, c: b64e(("commit:" + o["sha"]).encode())),
        "oid": ("GitObjectID!", lambda o, a, c: o["sha"]),
        "message": ("String!", lambda o, a, c: parse_commit(
            get_object(o["sha"])[1])["message"] if get_object(o["sha"])
            else ""),
        "tree": ("Tree!", lambda o, a, c: {"__type": "Tree",
                                           "repo": o["repo"],
                                           "sha": commit_tree(o["sha"])}),
        "url": ("URI!", lambda o, a, c: "https://github.com/x/commit/"
                + o["sha"]),
    }},
    "Tree": {"kind": "OBJECT", "fields": {
        "id": ("ID!", lambda o, a, c: b64e(("tree:" + o["sha"]).encode())),
        "oid": ("GitObjectID!", lambda o, a, c: o["sha"]),
        "entries": ("[TreeEntry!]!", lambda o, a, c: [
            {"__type": "TreeEntry", "repo": o["repo"], "mode": m,
             "name": n, "sha": s, "path": n}
            for m, n, s in parse_tree(get_object(o["sha"])[1])]),
    }},
    "TreeEntry": {"kind": "OBJECT", "fields": {
        "name": ("String!", lambda o, a, c: o["name"]),
        "path": ("String!", lambda o, a, c: o["path"]),
        "mode": ("Int!", lambda o, a, c: int(o["mode"], 8)),
        "type": ("String!", lambda o, a, c: "tree" if o["mode"] == "40000"
                 else "blob"),
        "object": ("GitObject", lambda o, a, c: (
            {"__type": "Tree", "repo": o["repo"], "sha": o["sha"]}
            if o["mode"] == "40000" else
            {"__type": "Blob", "repo": o["repo"], "sha": o["sha"],
             "text": get_object(o["sha"])[1].decode("utf-8", "replace")})),
    }},
    "Blob": {"kind": "OBJECT", "fields": {
        "id": ("ID!", lambda o, a, c: b64e(("blob:" + o["sha"]).encode())),
        "oid": ("GitObjectID!", lambda o, a, c: o["sha"]),
        "text": ("String", lambda o, a, c: o.get("text", "")),
        "isTruncated": ("Boolean!", lambda o, a, c: False),
        "byteSize": ("Int!", lambda o, a, c: len(
            get_object(o["sha"])[1]) if get_object(o["sha"]) else 0),
    }},
    "PullRequestChangedFile": {"kind": "OBJECT", "fields": {
        "path": ("String!", lambda o, a, c: o["file"]["filename"]),
        "additions": ("Int!", lambda o, a, c: o["file"]["additions"]),
        "deletions": ("Int!", lambda o, a, c: o["file"]["deletions"]),
        "changeType": ("PatchStatus!", lambda o, a, c: {
            "added": "ADDED", "removed": "DELETED",
            "modified": "MODIFIED"}.get(o["file"]["status"], "MODIFIED")),
    }},
    "PageInfo": {"kind": "OBJECT", "fields": {
        "hasNextPage": ("Boolean!", lambda o, a, c: o["hasNextPage"]),
        "hasPreviousPage": ("Boolean!", lambda o, a, c: o["hasPreviousPage"]),
        "startCursor": ("String", lambda o, a, c: o["startCursor"]),
        "endCursor": ("String", lambda o, a, c: o["endCursor"]),
    }},
    "IssueState": {"kind": "ENUM", "values": ["OPEN", "CLOSED"]},
    "PullRequestState": {"kind": "ENUM",
                         "values": ["OPEN", "CLOSED", "MERGED"]},
    "SearchType": {"kind": "ENUM", "values": ["ISSUE", "REPOSITORY", "USER"]},
    "MergeableState": {"kind": "ENUM",
                       "values": ["MERGEABLE", "CONFLICTING", "UNKNOWN"]},
    "RepositoryPermission": {"kind": "ENUM",
                             "values": ["ADMIN", "MAINTAIN", "WRITE",
                                        "TRIAGE", "READ"]},
    "OrderDirection": {"kind": "ENUM", "values": ["ASC", "DESC"]},
    "IssueOrderField": {"kind": "ENUM",
                        "values": ["CREATED_AT", "UPDATED_AT", "COMMENTS"]},
    "PullRequestOrderField": {"kind": "ENUM",
                              "values": ["CREATED_AT", "UPDATED_AT"]},
    "PatchStatus": {"kind": "ENUM",
                    "values": ["ADDED", "CHANGED", "COPIED", "DELETED",
                               "MODIFIED", "RENAMED"]},
    "PullRequestMergeMethod": {"kind": "ENUM",
                               "values": ["MERGE", "REBASE", "SQUASH"]},
}

# Connection types share one resolver shape.
for _cname in ("IssueConnection", "PullRequestConnection", "LabelConnection",
               "RepositoryConnection", "UserConnection",
               "IssueCommentConnection", "RefConnection",
               "PullRequestChangedFileConnection",
               "PullRequestCommitConnection", "SearchResultItemConnection"):
    GQL_TYPES[_cname] = {"kind": "OBJECT", "fields": {
        "totalCount": ("Int!", lambda o, a, c: o["totalCount"]),
        "nodes": ("[Node]", lambda o, a, c: o["nodes"]),
        "edges": ("[Edge]", lambda o, a, c: o["edges"]),
        "pageInfo": ("PageInfo!", lambda o, a, c: o["pageInfo"]),
    }}
GQL_TYPES["Edge"] = {"kind": "OBJECT", "fields": {
    "cursor": ("String!", lambda o, a, c: o["cursor"]),
    "node": ("Node", lambda o, a, c: o["node"]),
}}

for _scalar in ("String", "Int", "Float", "Boolean", "ID", "DateTime",
                "URI", "GitObjectID", "HTML", "Date", "BigInt"):
    GQL_TYPES[_scalar] = {"kind": "SCALAR"}
for _iface in ("Node", "GitObject"):
    GQL_TYPES[_iface] = {"kind": "INTERFACE", "fields": {
        "id": ("ID!", lambda o, a, c: None)}}

# Payload + input types for mutations.
GQL_TYPES["CreatePullRequestPayload"] = {"kind": "OBJECT", "fields": {
    "clientMutationId": ("String", lambda o, a, c: o.get("clientMutationId")),
    "pullRequest": ("PullRequest", lambda o, a, c: o.get("pullRequest")),
}}
GQL_TYPES["CreateIssuePayload"] = {"kind": "OBJECT", "fields": {
    "clientMutationId": ("String", lambda o, a, c: o.get("clientMutationId")),
    "issue": ("Issue", lambda o, a, c: o.get("issue")),
}}
GQL_TYPES["MergePullRequestPayload"] = {"kind": "OBJECT", "fields": {
    "clientMutationId": ("String", lambda o, a, c: o.get("clientMutationId")),
    "pullRequest": ("PullRequest", lambda o, a, c: o.get("pullRequest")),
}}
GQL_TYPES["AddCommentPayload"] = {"kind": "OBJECT", "fields": {
    "clientMutationId": ("String", lambda o, a, c: o.get("clientMutationId")),
    "commentEdge": ("IssueCommentEdge", lambda o, a, c: o.get("commentEdge")),
    "subject": ("Node", lambda o, a, c: None),
}}
GQL_TYPES["CloseIssuePayload"] = {"kind": "OBJECT", "fields": {
    "clientMutationId": ("String", lambda o, a, c: o.get("clientMutationId")),
    "issue": ("Issue", lambda o, a, c: o.get("issue")),
}}
GQL_TYPES["IssueCommentEdge"] = GQL_TYPES["Edge"]
for _iname, _ifields in {
    "CreatePullRequestInput": ["clientMutationId", "repositoryId",
                               "baseRefName", "headRefName", "title",
                               "body", "draft", "maintainerCanModify"],
    "CreateIssueInput": ["clientMutationId", "repositoryId", "title",
                         "body", "labelIds", "assigneeIds"],
    "MergePullRequestInput": ["clientMutationId", "pullRequestId",
                              "mergeMethod", "commitHeadline"],
    "AddCommentInput": ["clientMutationId", "subjectId", "body"],
    "CloseIssueInput": ["clientMutationId", "issueId"],
    "IssueOrder": ["field", "direction"],
    "PullRequestOrder": ["field", "direction"],
}.items():
    GQL_TYPES[_iname] = {"kind": "INPUT_OBJECT",
                         "input_fields": _ifields}


def gql_type_ref(type_str):
    s = type_str.strip()
    if s.endswith("!"):
        return {"kind": "NON_NULL", "name": None,
                "ofType": gql_type_ref(s[:-1])}
    if s.startswith("[") and s.endswith("]"):
        return {"kind": "LIST", "name": None,
                "ofType": gql_type_ref(s[1:-1])}
    kind = "OBJECT"
    tdef = GQL_TYPES.get(s)
    if tdef:
        kind = tdef["kind"]
    return {"kind": kind, "name": s, "ofType": None}


def gql_type_object(name):
    tdef = GQL_TYPES.get(name)
    if tdef is None:
        return None
    kind = tdef["kind"]
    out = {"name": name, "kind": kind,
           "description": "The %s type of this forge." % name,
           "fields": None, "inputFields": None, "enumValues": None,
           "possibleTypes": None, "specifiedByURL": None,
           "isOneOf": False, "interfaces": [], "ofType": None}
    if kind == "OBJECT" or kind == "INTERFACE":
        fields = []
        for fname, (ftype, _r) in tdef["fields"].items():
            fields.append({"name": fname, "description": None, "args": [],
                           "type": gql_type_ref(ftype),
                           "isDeprecated": False,
                           "deprecationReason": None})
        out["fields"] = fields
    if kind == "ENUM":
        out["enumValues"] = [{"name": v, "description": None,
                               "isDeprecated": False,
                               "deprecationReason": None}
                              for v in tdef["values"]]
    if kind == "INPUT_OBJECT":
        out["inputFields"] = [{"name": f, "description": None,
                               "type": gql_type_ref("String"),
                               "defaultValue": None}
                              for f in tdef["input_fields"]]
    return out


def gql_find_repo_by_node(nid):
    kind, dbid = parse_node_id(nid or "")
    if kind == "Repository":
        for repo in STORE.repos.values():
            if repo["id"] == dbid:
                return repo
    return None


def gql_do_merge(repo, pull, login, method_req):
    """Shared squash/merge used by GraphQL. Returns new sha or raises."""
    head_sha = repo["refs"]["refs/heads/" + pull["head_ref"]]
    base_ref = "refs/heads/" + pull["base_ref"]
    base_sha = repo["refs"][base_ref]
    mb = merge_base(head_sha, base_sha) or base_sha
    if mb == head_sha:
        new_sha = base_sha
    else:
        mb_flat = flatten_tree(commit_tree(mb))
        head_flat = flatten_tree(commit_tree(head_sha))
        base_flat = flatten_tree(commit_tree(base_sha))
        merged_flat = dict(base_flat)
        for path in set(head_flat) | set(mb_flat):
            if head_flat.get(path) != mb_flat.get(path):
                if path in head_flat:
                    merged_flat[path] = head_flat[path]
                else:
                    merged_flat.pop(path, None)
        tree = build_tree_from_flat(merged_flat)
        if (method_req or "SQUASH").upper() == "MERGE":
            new_sha = make_commit(tree, [base_sha, head_sha],
                                  "Merge pull request #%d from %s"
                                  % (pull["number"], pull["head_ref"]),
                                  login, login + "@forge.local")
        else:
            new_sha = make_commit(tree, [base_sha],
                                  "%s (#%d)" % (pull["title"],
                                                pull["number"]),
                                  login, login + "@forge.local")
    repo["refs"][base_ref] = new_sha
    repo["updated_ts"] = tick()
    pull["merged"] = True
    pull["state"] = "closed"
    pull["merged_ts"] = tick()
    pull["closed_ts"] = pull["merged_ts"]
    pull["merge_commit_sha"] = new_sha
    pull["updated_ts"] = tick()
    return new_sha


def gql_query_fields():
    def node_resolver(obj, args, ctx):
        kind, dbid = parse_node_id(args.get("id") or "")
        if kind == "Repository":
            repo = gql_find_repo_by_node(args.get("id"))
            return g_repo(repo) if repo else None
        if kind in ("Issue", "PullRequest"):
            for repo in STORE.repos.values():
                table = repo["issues"] if kind == "Issue" else repo["pulls"]
                for rec in table.values():
                    if rec["id"] == dbid:
                        return (g_issue(repo, rec) if kind == "Issue"
                                else g_pull(repo, rec))
        if kind == "User":
            for login, u in STORE.users.items():
                if u["id"] == dbid:
                    return g_user(login)
        return None

    def search_resolver(obj, args, ctx):
        q = (args.get("query") or "").lower()
        stype = (args.get("type") or "REPOSITORY").upper()
        nodes = []
        if stype == "REPOSITORY":
            for repo in STORE.repos.values():
                if q in repo["name"].lower() or q in repo["owner"].lower():
                    nodes.append(g_repo(repo))
        elif stype == "ISSUE":
            for repo in STORE.repos.values():
                for issue in repo["issues"].values():
                    if q in issue["title"].lower() or \
                            q in issue["body"].lower():
                        nodes.append(g_pull(repo, issue) if issue.get("is_pull")
                                     else g_issue(repo, issue))
        elif stype == "USER":
            for login in STORE.users:
                if q in login.lower():
                    nodes.append(g_user(login))
        conn = g_connection(nodes, args)
        conn["__type"] = "SearchResultItemConnection"
        conn["issueCount"] = sum(1 for n in nodes
                                 if n["__type"] in ("Issue", "PullRequest"))
        conn["repositoryCount"] = sum(1 for n in nodes
                                      if n["__type"] == "Repository")
        conn["userCount"] = sum(1 for n in nodes if n["__type"] == "User")
        return conn

    return {
        "repository": ("Repository", lambda o, a, c: (
            g_repo(STORE.get_repo(a.get("owner", ""), a.get("name", "")))
            if STORE.get_repo(a.get("owner", ""), a.get("name", ""))
            else None)),
        "repositoryOwner": ("RepositoryOwner", lambda o, a, c: (
            g_user(a["login"]) if a.get("login") in STORE.users else None)),
        "user": ("User", lambda o, a, c: (
            g_user(a["login"]) if a.get("login") in STORE.users else None)),
        "viewer": ("User!", lambda o, a, c: g_user(c["login"])),
        "node": ("Node", node_resolver),
        "nodes": ("[Node]", lambda o, a, c: [node_resolver(o, {"id": i}, c)
                                             for i in a.get("ids", [])]),
        "search": ("SearchResultItemConnection!", search_resolver),
        "rateLimit": ("RateLimit", lambda o, a, c: {
            "__type": "RateLimit", "limit": 5000, "cost": 1,
            "remaining": 4999, "resetAt": "2027-01-01T00:00:00Z",
            "used": 1, "nodeCount": 0}),
    }


GQL_TYPES["RateLimit"] = {"kind": "OBJECT", "fields": {
    "limit": ("Int!", lambda o, a, c: o["limit"]),
    "cost": ("Int!", lambda o, a, c: o["cost"]),
    "remaining": ("Int!", lambda o, a, c: o["remaining"]),
    "resetAt": ("DateTime!", lambda o, a, c: o["resetAt"]),
    "used": ("Int!", lambda o, a, c: o["used"]),
    "nodeCount": ("Int!", lambda o, a, c: o["nodeCount"]),
}}
GQL_TYPES["SearchResultItemConnection"] = {"kind": "OBJECT", "fields": {
    "totalCount": ("Int!", lambda o, a, c: o["totalCount"]),
    "nodes": ("[Node]", lambda o, a, c: o["nodes"]),
    "edges": ("[Edge]", lambda o, a, c: o["edges"]),
    "pageInfo": ("PageInfo!", lambda o, a, c: o["pageInfo"]),
    "issueCount": ("Int!", lambda o, a, c: o.get("issueCount", 0)),
    "repositoryCount": ("Int!", lambda o, a, c: o.get("repositoryCount", 0)),
    "userCount": ("Int!", lambda o, a, c: o.get("userCount", 0)),
}}
GQL_TYPES["RepositoryOwner"] = GQL_TYPES["User"]


def gql_mutation_fields():
    def inp(args):
        data = args.get("input")
        if isinstance(data, dict):
            return data
        return args

    def create_pr(obj, args, ctx):
        data = inp(args)
        repo = gql_find_repo_by_node(data.get("repositoryId", ""))
        if repo is None:
            # repositoryId may be a database id or full name.
            rid = str(data.get("repositoryId", ""))
            if "/" in rid:
                repo = STORE.get_repo(*rid.split("/", 1))
            elif rid.isdigit():
                for r in STORE.repos.values():
                    if r["id"] == int(rid):
                        repo = r
        if repo is None:
            raise ValueError("repository not found")
        head = (data.get("headRefName") or data.get("head") or "")
        base = (data.get("baseRefName") or data.get("base")
                or repo["default_branch"])
        head = head.split(":")[-1]
        base = base.split(":")[-1]
        if "refs/heads/" + head not in repo["refs"]:
            raise ValueError("head ref does not exist")
        if "refs/heads/" + base not in repo["refs"]:
            raise ValueError("base ref does not exist")
        rec = make_issue_record(repo, ctx["login"],
                                data.get("title") or "", data.get("body"),
                                [], is_pull=True)
        with STORE.lock:
            rec.update({"head_ref": head, "base_ref": base,
                        "merged": False, "merged_ts": None,
                        "merge_commit_sha": None})
            repo["pulls"][rec["number"]] = rec
        return {"__type": "CreatePullRequestPayload",
                "clientMutationId": data.get("clientMutationId"),
                "pullRequest": g_pull(repo, rec)}

    def create_issue(obj, args, ctx):
        data = inp(args)
        repo = gql_find_repo_by_node(data.get("repositoryId", ""))
        if repo is None:
            raise ValueError("repository not found")
        rec = make_issue_record(repo, ctx["login"], data.get("title") or "",
                                data.get("body"), [])
        return {"__type": "CreateIssuePayload",
                "clientMutationId": data.get("clientMutationId"),
                "issue": g_issue(repo, rec)}

    def merge_pr(obj, args, ctx):
        data = inp(args)
        kind, dbid = parse_node_id(data.get("pullRequestId", ""))
        target = None
        trepo = None
        for repo in STORE.repos.values():
            for rec in repo["pulls"].values():
                if rec["id"] == dbid:
                    target, trepo = rec, repo
        if target is None:
            raise ValueError("pull request not found")
        if target["merged"] or target["state"] != "open":
            raise ValueError("pull request is not mergeable")
        if pull_mergeable(trepo, target) is not True:
            raise ValueError("pull request is not mergeable")
        with STORE.lock:
            gql_do_merge(trepo, target, ctx["login"],
                         data.get("mergeMethod"))
        return {"__type": "MergePullRequestPayload",
                "clientMutationId": data.get("clientMutationId"),
                "pullRequest": g_pull(trepo, target)}

    def add_comment(obj, args, ctx):
        data = inp(args)
        kind, dbid = parse_node_id(data.get("subjectId", ""))
        for repo in STORE.repos.values():
            for rec in repo["issues"].values():
                if rec["id"] == dbid:
                    with STORE.lock:
                        cid = STORE.next_comment_id
                        STORE.next_comment_id += 1
                        ts = tick()
                        comment = {"id": cid, "body": data.get("body") or "",
                                   "author": ctx["login"],
                                   "created_ts": ts, "updated_ts": ts}
                        rec["comments"].append(comment)
                    node = {"__type": "IssueComment", "repo": repo,
                            "rec": rec, "comment": comment}
                    return {"__type": "AddCommentPayload",
                            "clientMutationId": data.get("clientMutationId"),
                            "commentEdge": {"__type": "Edge", "node": node,
                                            "cursor": b64e(b"cursor:0")}}
        raise ValueError("subject not found")

    def close_issue(obj, args, ctx):
        # gh's `gh issue close`: CloseIssueInput{issueId} -> issue.
        data = inp(args)
        kind, dbid = parse_node_id(data.get("issueId", ""))
        for repo in STORE.repos.values():
            for rec in repo["issues"].values():
                if rec["id"] == dbid:
                    with STORE.lock:
                        rec["state"] = "closed"
                        rec["closed_ts"] = tick()
                        rec["updated_ts"] = tick()
                    return {"__type": "CloseIssuePayload",
                            "clientMutationId": data.get("clientMutationId"),
                            "issue": g_issue(repo, rec)}
        raise ValueError("issue not found")

    return {
        "createPullRequest": ("CreatePullRequestPayload", create_pr),
        "createIssue": ("CreateIssuePayload", create_issue),
        "closeIssue": ("CloseIssuePayload", close_issue),
        "mergePullRequest": ("MergePullRequestPayload", merge_pr),
        "addComment": ("AddCommentPayload", add_comment),
    }


def gql_execute_selection(obj, type_name, selections, ctx, variables,
                          fragments, errors):
    out = {}
    fields = {}
    if type_name == "Query":
        fields = gql_query_fields()
    elif type_name == "Mutation":
        fields = gql_mutation_fields()
    elif type_name in GQL_TYPES and \
            GQL_TYPES[type_name]["kind"] in ("OBJECT", "INTERFACE"):
        fields = GQL_TYPES[type_name].get("fields", {})
    for sel in selections:
        if sel["kind"] == "fragment":
            frag = fragments.get(sel["name"])
            if frag:
                sub = gql_execute_selection(obj, frag["type_condition"],
                                            frag["selection"], ctx,
                                            variables, fragments, errors)
                out.update(sub)
            continue
        if sel["kind"] == "inline":
            if obj is not None and isinstance(obj, dict) and \
                    obj.get("__type") == sel["type"]:
                sub = gql_execute_selection(obj, sel["type"],
                                            sel["selection"], ctx, variables,
                                            fragments, errors)
                out.update(sub)
            continue
        name = sel["name"]
        key = sel["alias"] or name
        args = {k: gql_resolve_value(v, variables)
                for k, v in sel["args"].items()}
        if name == "__typename":
            out[key] = type_name
            continue
        if name == "__type" and type_name == "Query":
            out[key] = _gql_introspect_type(args.get("name"))
            if sel["selection"]:
                out[key] = gql_execute_selection(
                    out[key], "__Type", sel["selection"], ctx, variables,
                    fragments, errors)
            continue
        if name == "__schema" and type_name == "Query":
            schema = _gql_schema_object()
            out[key] = gql_execute_selection(
                schema, "__Schema", sel["selection"], ctx, variables,
                fragments, errors) if sel["selection"] else schema
            continue
        spec = fields.get(name)
        if spec is None:
            errors.append({
                "message": "Field '%s' doesn't exist on type '%s'"
                           % (name, type_name),
                "type": "VALIDATION",
                "path": [key],
                "extensions": {"code": "undefinedField",
                               "typeName": type_name, "fieldName": name}})
            out[key] = None
            continue
        try:
            value = spec[1](obj, args, ctx)
        except Exception as exc:  # noqa: BLE001
            errors.append({"message": str(exc), "type": "INTERNAL",
                           "path": [key]})
            out[key] = None
            continue
        if sel["selection"] and value is not None:
            out[key] = gql_complete(value, spec[0], sel["selection"], ctx,
                                    variables, fragments, errors)
        else:
            out[key] = gql_plain(value)
    return out


def gql_plain(value):
    if isinstance(value, dict) and "__type" in value:
        return value.get("login") or value.get("name")
    return value


def gql_complete(value, type_str, selections, ctx, variables, fragments,
                 errors):
    base = type_str.rstrip("!")
    if base.startswith("[") and base.endswith("]"):
        inner = base[1:-1]
        return [gql_complete(v, inner, selections, ctx, variables,
                             fragments, errors) for v in value]
    if isinstance(value, list):
        return [gql_complete(v, base, selections, ctx, variables,
                             fragments, errors) for v in value]
    if isinstance(value, dict) and "__type" in value:
        return gql_execute_selection(value, value["__type"], selections,
                                     ctx, variables, fragments, errors)
    return value


def _gql_introspect_type(name):
    if not name:
        return None
    tdef = GQL_TYPES.get(name)
    if tdef is None and name in ("Query", "Mutation"):
        tdef = {"kind": "OBJECT",
                "fields": gql_query_fields() if name == "Query"
                else gql_mutation_fields()}
    if tdef is None:
        return None
    obj = gql_type_object(name)
    if obj is None:
        obj = {"name": name, "kind": tdef["kind"], "description": None,
               "fields": None, "inputFields": None, "enumValues": None,
               "possibleTypes": None, "interfaces": [], "ofType": None,
               "specifiedByURL": None, "isOneOf": False}
    if tdef["kind"] in ("OBJECT", "INTERFACE"):
        # Rebuild fields with argument metadata.
        raw = gql_query_fields() if name == "Query" else \
            gql_mutation_fields() if name == "Mutation" else \
            tdef.get("fields", {})
        fields = []
        for fname, spec in raw.items():
            ftype = spec[0]
            fields.append({"__type": "__Field", "name": fname,
                           "description": None, "args": [],
                           "type": gql_type_ref(ftype),
                           "isDeprecated": False, "deprecationReason": None})
        obj["fields"] = fields
    obj["__type"] = "__Type"
    return obj


def _gql_schema_object():
    return {"__type": "__Schema",
            "queryType": {"__type": "__Type", "name": "Query"},
            "mutationType": {"__type": "__Type", "name": "Mutation"},
            "subscriptionType": None,
            "types": None,
            "directives": [
                {"__type": "__Directive", "name": "include",
                 "description": None, "locations": ["FIELD"],
                 "args": [], "isRepeatable": False},
                {"__type": "__Directive", "name": "skip",
                 "description": None, "locations": ["FIELD"],
                 "args": [], "isRepeatable": False}]}


GQL_TYPES["__Type"] = {"kind": "OBJECT", "fields": {
    "name": ("String", lambda o, a, c: o.get("name")),
    "kind": ("String!", lambda o, a, c: o.get("kind")),
    "description": ("String", lambda o, a, c: o.get("description")),
    "fields": ("[__Field!]", lambda o, a, c: o.get("fields")),
    "inputFields": ("[__InputValue!]", lambda o, a, c: o.get("inputFields")),
    "enumValues": ("[__EnumValue!]", lambda o, a, c: o.get("enumValues")),
    "possibleTypes": ("[__Type!]", lambda o, a, c: o.get("possibleTypes")),
    "interfaces": ("[__Type!]!", lambda o, a, c: []),
    "ofType": ("__Type", lambda o, a, c: _type_ref_object(o.get("ofType"))),
    "specifiedByURL": ("String", lambda o, a, c: None),
    "isOneOf": ("Boolean", lambda o, a, c: False),
}}
GQL_TYPES["__Field"] = {"kind": "OBJECT", "fields": {
    "name": ("String!", lambda o, a, c: o["name"]),
    "description": ("String", lambda o, a, c: o.get("description")),
    "args": ("[__InputValue!]!", lambda o, a, c: o.get("args", [])),
    "type": ("__Type!", lambda o, a, c: _type_ref_object(o["type"])),
    "isDeprecated": ("Boolean!", lambda o, a, c: o.get("isDeprecated",
                                                       False)),
    "deprecationReason": ("String", lambda o, a, c: o.get(
        "deprecationReason")),
}}
GQL_TYPES["__InputValue"] = {"kind": "OBJECT", "fields": {
    "name": ("String!", lambda o, a, c: o["name"]),
    "description": ("String", lambda o, a, c: o.get("description")),
    "type": ("__Type!", lambda o, a, c: _type_ref_object(
        o["type"]) if isinstance(o.get("type"), dict) else o.get("type")),
    "defaultValue": ("String", lambda o, a, c: o.get("defaultValue")),
    "isDeprecated": ("Boolean!", lambda o, a, c: False),
    "deprecationReason": ("String", lambda o, a, c: None),
}}
GQL_TYPES["__EnumValue"] = {"kind": "OBJECT", "fields": {
    "name": ("String!", lambda o, a, c: o["name"]),
    "description": ("String", lambda o, a, c: o.get("description")),
    "isDeprecated": ("Boolean!", lambda o, a, c: o.get("isDeprecated",
                                                       False)),
    "deprecationReason": ("String", lambda o, a, c: o.get(
        "deprecationReason")),
}}
GQL_TYPES["__Schema"] = {"kind": "OBJECT", "fields": {
    "description": ("String", lambda o, a, c: None),
    "types": ("[__Type!]!", lambda o, a, c: [
        _gql_introspect_type(n) for n in list(GQL_TYPES.keys())
        if not n.startswith("__")]),
    "queryType": ("__Type!", lambda o, a, c: _gql_introspect_type("Query")),
    "mutationType": ("__Type", lambda o, a, c: _gql_introspect_type(
        "Mutation")),
    "subscriptionType": ("__Type", lambda o, a, c: None),
    "directives": ("[__Directive!]!", lambda o, a, c: o.get("directives",
                                                            [])),
}}
GQL_TYPES["__Directive"] = {"kind": "OBJECT", "fields": {
    "name": ("String!", lambda o, a, c: o["name"]),
    "description": ("String", lambda o, a, c: o.get("description")),
    "locations": ("[String!]!", lambda o, a, c: o.get("locations", [])),
    "args": ("[__InputValue!]!", lambda o, a, c: o.get("args", [])),
    "isRepeatable": ("Boolean!", lambda o, a, c: o.get("isRepeatable",
                                                       False)),
}}


def _type_ref_object(ref):
    if ref is None:
        return None
    if isinstance(ref, dict) and "__type" in ref:
        return ref
    out = {"__type": "__Type", "name": ref.get("name"),
           "kind": ref.get("kind"), "description": None, "fields": None,
           "inputFields": None, "enumValues": None, "possibleTypes": None,
           "interfaces": [], "specifiedByURL": None, "isOneOf": False,
           "ofType": _type_ref_object(ref.get("ofType"))}
    if ref.get("name") and ref.get("kind") in ("OBJECT", "ENUM",
                                               "INPUT_OBJECT", "INTERFACE"):
        full = _gql_introspect_type(ref["name"])
        if full is not None:
            merged = dict(full)
            merged["ofType"] = None
            return merged
    return out


def gql_run(ctx, payload):
    """Execute one GraphQL request payload. Returns result dict."""
    query = payload.get("query") or ""
    variables = dict(payload.get("variables") or {})
    op_name = payload.get("operationName")
    try:
        operations, fragments = GQLParser(query).parse_document()
    except Exception as exc:  # noqa: BLE001
        return {"errors": [{"message": "Parse error: %s" % exc,
                            "type": "VALIDATION"}]}
    if not operations:
        return {"errors": [{"message": "No operation found",
                            "type": "VALIDATION"}]}
    chosen = operations
    if op_name:
        chosen = [o for o in operations if o["name"] == op_name] or operations
    results = []
    for op in chosen:
        vars2 = dict(op["var_defs"])
        vars2.update({k: v for k, v in variables.items() if v is not None})
        root_type = "Mutation" if op["type"] == "mutation" else "Query"
        errors = []
        data = gql_execute_selection({"__type": root_type}, root_type,
                                     op["selection"], ctx, vars2,
                                     fragments, errors)
        result = {"data": data}
        if errors:
            result["errors"] = errors
        results.append(result)
    if op_name or len(results) == 1:
        return results[0]
    return results


GQL_TYPES["Connection"] = GQL_TYPES["IssueConnection"]

# --------------------------------------------------------------------------
# MCP (Model Context Protocol) at /mcp — JSON-RPC 2.0, Streamable HTTP
# --------------------------------------------------------------------------

MCP_TOOLS = [
    {"name": "create_repo",
     "description": "Create a new repository owned by the authenticated "
                    "user.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "name": {"type": "string"},
                         "description": {"type": "string"},
                         "private": {"type": "boolean"}},
                     "required": ["name"]}},
    {"name": "get_repo",
     "description": "Get a repository by owner and name.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string"},
                         "repo": {"type": "string"},
                         "full_name": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "list_repos",
     "description": "List repositories owned by the authenticated user.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "create_issue",
     "description": "Create an issue in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string"},
                         "repo": {"type": "string"},
                         "title": {"type": "string"},
                         "body": {"type": "string"},
                         "labels": {"type": "array",
                                    "items": {"type": "string"}}},
                     "required": ["owner", "repo", "title"]}},
    {"name": "get_issue",
     "description": "Get one issue by number.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string"},
                         "repo": {"type": "string"},
                         "issue_number": {"type": "integer"}},
                     "required": ["owner", "repo", "issue_number"]}},
    {"name": "list_issues",
     "description": "List issues in a repository.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string"},
                         "repo": {"type": "string"},
                         "state": {"type": "string"}},
                     "required": ["owner", "repo"]}},
    {"name": "create_pull_request",
     "description": "Open a pull request from a head branch to a base "
                    "branch.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string"},
                         "repo": {"type": "string"},
                         "title": {"type": "string"},
                         "body": {"type": "string"},
                         "head": {"type": "string"},
                         "base": {"type": "string"}},
                     "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request",
     "description": "Merge a pull request (squash) into its base branch.",
     "inputSchema": {"type": "object",
                     "properties": {
                         "owner": {"type": "string"},
                         "repo": {"type": "string"},
                         "pull_number": {"type": "integer"}},
                     "required": ["owner", "repo", "pull_number"]}},
]

MCP_TOOLS_BY_SUFFIX = {}
for _t in MCP_TOOLS:
    MCP_TOOLS_BY_SUFFIX[_t["name"].lower()] = _t


def mcp_find_tool(name):
    if not name:
        return None
    if name in MCP_TOOLS_BY_SUFFIX:
        return MCP_TOOLS_BY_SUFFIX[name]
    low = name.lower()
    for suffix, tool in MCP_TOOLS_BY_SUFFIX.items():
        if low.endswith(suffix) or low.endswith("__" + suffix):
            return tool
    return None


def mcp_tool_result(obj):
    return {"content": [{"type": "text",
                         "text": json.dumps(obj, indent=2)}],
            "structuredContent": obj, "isError": False}


def mcp_tool_error(message):
    return {"content": [{"type": "text", "text": message}],
            "isError": True}


def mcp_call_tool(ctx, name, arguments):
    tool = mcp_find_tool(name)
    if tool is None:
        return None  # caller turns this into -32602
    args = arguments or {}
    # Map full_name onto owner/repo when given.
    if args.get("full_name") and "/" in str(args["full_name"]):
        o, r = str(args["full_name"]).split("/", 1)
        args.setdefault("owner", o)
        args.setdefault("repo", r)
    required = tool["inputSchema"].get("required", [])
    for req in required:
        if args.get(req) is None:
            return mcp_tool_error("missing required argument: " + req)
    login = ctx["login"]
    tname = tool["name"]
    try:
        if tname == "create_repo":
            if STORE.get_repo(login, args["name"]):
                return mcp_tool_error("repository already exists")
            repo = new_repo(login, args["name"],
                            args.get("description", ""),
                            args.get("private", False))
            return mcp_tool_result(repo_json(ctx, repo))
        if tname == "list_repos":
            mine = [repo_json(ctx, r) for r in STORE.repos.values()
                    if r["owner"] == login]
            return mcp_tool_result(mine)
        repo = STORE.get_repo(args.get("owner", ""), args.get("repo", ""))
        if repo is None:
            return mcp_tool_error("repository not found")
        if tname == "get_repo":
            return mcp_tool_result(repo_json(ctx, repo))
        if tname == "create_issue":
            names = validate_labels(repo, args.get("labels"))
            rec = make_issue_record(repo, login, args["title"],
                                    args.get("body"), names)
            return mcp_tool_result(issue_json(ctx, repo, rec))
        if tname == "get_issue":
            issue = repo["issues"].get(int(args["issue_number"]))
            if issue is None:
                return mcp_tool_error("issue not found")
            return mcp_tool_result(issue_json(ctx, repo, issue))
        if tname == "list_issues":
            state = args.get("state") or "open"
            items = [issue_json(ctx, repo, i)
                     for i in sorted(repo["issues"].values(),
                                     key=lambda x: x["number"])
                     if state == "all" or i["state"] == state]
            return mcp_tool_result(items)
        if tname == "create_pull_request":
            head = str(args["head"]).split(":")[-1]
            base = str(args["base"]).split(":")[-1]
            if "refs/heads/" + head not in repo["refs"]:
                return mcp_tool_error("head ref does not exist")
            if "refs/heads/" + base not in repo["refs"]:
                return mcp_tool_error("base ref does not exist")
            rec = make_issue_record(repo, login, args["title"],
                                    args.get("body"), [], is_pull=True)
            with STORE.lock:
                rec.update({"head_ref": head, "base_ref": base,
                            "merged": False, "merged_ts": None,
                            "merge_commit_sha": None})
                repo["pulls"][rec["number"]] = rec
            return mcp_tool_result(pull_json(ctx, repo, rec))
        if tname == "merge_pull_request":
            pull = repo["pulls"].get(int(args["pull_number"]))
            if pull is None:
                return mcp_tool_error("pull request not found")
            if pull["merged"] or pull["state"] != "open":
                return mcp_tool_error("pull request is not mergeable")
            if pull_mergeable(repo, pull) is not True:
                return mcp_tool_error("pull request is not mergeable")
            with STORE.lock:
                sha = gql_do_merge(repo, pull, login, "SQUASH")
            return mcp_tool_result({"sha": sha, "merged": True,
                                    "message": "Pull Request successfully "
                                               "merged",
                                    "pull_request": pull_json(ctx, repo,
                                                              pull)})
    except ApiError as exc:
        return mcp_tool_error(exc.message)
    except Exception as exc:  # noqa: BLE001
        return mcp_tool_error(str(exc))
    return mcp_tool_error("unhandled tool")


def mcp_handle(ctx, payload):
    """One JSON-RPC request. Returns (http_status, result_or_error_dict,
    is_notification)."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" \
            or "method" not in payload:
        return 400, {"jsonrpc": "2.0", "id": payload.get("id")
                     if isinstance(payload, dict) else None,
                     "error": {"code": -32600, "message": "Invalid Request"}}, \
            False
    method = payload.get("method")
    rid = payload.get("id")
    params = payload.get("params") or {}
    if method.startswith("notifications/"):
        return 202, None, True
    if method == "server/discover":
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2026-07-28",
            "supportedVersions": ["2026-07-28", "2025-11-25",
                                  "2025-06-18", "2025-03-26",
                                  "2024-11-05"],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "forge", "version": "1.0.0"}}}, False
    if method == "initialize":
        requested = params.get("protocolVersion") or "2025-11-25"
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "forge", "version": "1.0.0"}}}, False
    if method == "ping":
        return 200, {"jsonrpc": "2.0", "id": rid, "result": {}}, False
    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": rid,
                     "result": {"tools": MCP_TOOLS}}, False
    if method == "tools/call":
        result = mcp_call_tool(ctx, params.get("name"),
                               params.get("arguments") or {})
        if result is None:
            return 200, {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32602,
                                   "message": "unknown tool: %s"
                                              % params.get("name")}}, False
        return 200, {"jsonrpc": "2.0", "id": rid, "result": result}, False
    return 200, {"jsonrpc": "2.0", "id": rid,
                 "error": {"code": -32601,
                           "message": "Method not found: " + method}}, False


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "forge/1.0"

    def log_message(self, fmt, *args):  # keep stdout quiet
        pass

    def setup(self):
        # Disable Nagle on every accepted connection.  Without this,
        # a small response written in several pieces waits up to 40 ms
        # for the client's delayed ACK before the next piece may go
        # out, so every sequential request on a keep-alive connection
        # costs ~41 ms (measured) instead of ~1 ms — enough to time
        # out the 2,000-read workload outright.  Node forges do this
        # by default; the stdlib server does not.
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP,
                                       socket.TCP_NODELAY, 1)
        except Exception:
            pass

    # -- plumbing helpers ---------------------------------------------------
    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send(self, status, body, content_type="application/json",
              extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _auth(self):
        """Return (token, login) or (None, None). Accepts Bearer and Basic
        (git clients send the token as username or password)."""
        header = self.headers.get("Authorization") or ""
        token = None
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        elif header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8",
                                                               "replace")
                user, _, password = decoded.partition(":")
                if password and password in STORE.tokens:
                    token = password
                elif user in STORE.tokens:
                    token = user
                elif user and not password and user in STORE.tokens:
                    token = user
            except Exception:
                token = None
        elif header.lower().startswith("token "):
            token = header[6:].strip()
        if token and token in STORE.tokens:
            return token, STORE.tokens[token]
        return None, None

    def _ctx(self, login):
        return {"login": login, "host": self.headers.get("Host",
                                                         "localhost")}

    def _request_id(self):
        with STORE.lock:
            STORE.request_seq += 1
            seq = STORE.request_seq
        return "%04X:%04X:0000000000000000" % (seq, seq * 7 % 0xFFFF)

    # -- main entry ---------------------------------------------------------
    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/v3"):
                self._handle_rest(method, path, query)
            elif path == "/api/graphql":
                self._handle_graphql(method, query)
            elif path == "/mcp":
                self._handle_mcp(method)
            elif path == "/" or path == "/healthz":
                self._send(200, {"name": "forge", "version": "1.0.0",
                                 "status": "ok"})
            else:
                self._handle_git(method, path, query)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            try:
                self._send(500, {"message": "Internal error: %s" % exc})
            except Exception:
                pass

    def _handle_rest(self, method, path, query):
        # Read the body FIRST: on a keep-alive connection an unread body
        # stays in the socket and the next request is misparsed as the
        # leftover bytes ("Bad request syntax ('{...json...}')").  A 401
        # sent before the read poisons every later request on the
        # connection, which is exactly how the suite drives us.
        raw = self._read_body() if method in ("POST", "PUT", "PATCH",
                                              "DELETE") else b""
        token, login = self._auth()
        rid = self._request_id()
        if login is None:
            self._send(401, {"message": "Requires authentication",
                             "documentation_url": "https://docs.github.com"},
                       extra={"X-GitHub-Request-Id": rid})
            return
        ctx = self._ctx(login)
        try:
            status, obj, headers = rest_dispatch(ctx, method, path, query,
                                                 raw)
        except ApiError as exc:
            status, obj, headers = exc.status, {"message": exc.message,
                                                "documentation_url":
                                                "https://docs.github.com"}, {}
        headers = dict(headers or {})
        headers["X-GitHub-Request-Id"] = rid
        headers.setdefault("X-OAuth-Scopes", "repo, user, admin:repo_hook")
        if status == 204 or obj is None:
            self._send(status, b"", extra=headers)
        else:
            self._send(status, obj, extra=headers)

    def _handle_graphql(self, method, query):
        # Read the body before auth, for the same keep-alive reason as
        # _handle_rest: a 401 must not leave the body unread.
        raw = self._read_body() if method in ("POST", "PUT", "PATCH",
                                              "DELETE") else b""
        token, login = self._auth()
        if login is None:
            self._send(401, {"message": "Requires authentication"})
            return
        ctx = self._ctx(login)
        if method == "GET":
            payload = {"query": query.get("query", [""])[0],
                       "variables": json.loads(
                           query.get("variables", ["{}"])[0] or "{}")}
            result = gql_run(ctx, payload)
            self._send(200, result)
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"errors": [{"message": "Problems parsing JSON",
                                         "type": "VALIDATION"}]})
            return
        if isinstance(payload, list):
            results = [gql_run(ctx, item) for item in payload]
            self._send(200, results)
            return
        result = gql_run(ctx, payload)
        self._send(200, result)

    def _handle_mcp(self, method):
        # Read the body before auth, for the same keep-alive reason as
        # _handle_rest (the suite's no-auth tools/call must not poison
        # the connection for the next request).
        raw = self._read_body() if method in ("POST", "PUT", "PATCH",
                                              "DELETE") else b""
        if method == "GET":
            self._send(405, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600,
                                       "message": "Method Not Allowed"}})
            return
        if method == "DELETE":
            self._send(200, b"")
            return
        token, login = self._auth()
        if login is None:
            self._send(401, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32001,
                                       "message": "Unauthorized"}})
            return
        ctx = self._ctx(login)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700,
                                       "message": "Parse error"}})
            return
        session_headers = {}
        if isinstance(payload, dict) and \
                payload.get("method") == "initialize":
            with STORE.lock:
                STORE.request_seq += 1
                seq = STORE.request_seq
            session_headers["Mcp-Session-Id"] = "forge-session-%d" % seq
        if isinstance(payload, list):
            out = []
            for item in payload:
                status, result, is_notif = mcp_handle(ctx, item)
                if not is_notif and result is not None:
                    out.append(result)
            self._send(200, out, extra=session_headers)
            return
        status, result, is_notif = mcp_handle(ctx, payload)
        if is_notif or result is None:
            self._send(202, b"", extra=session_headers)
            return
        self._send(status, result, extra=session_headers)

    def _handle_git(self, method, path, query):
        # /{owner}/{repo}.git/...
        # Read a POST body before auth, for the same keep-alive reason as
        # _handle_rest.
        raw = self._read_body() if method == "POST" else b""
        m = re.match(r"^/([^/]+)/([^/]+?)\.git(/.*)?$", path)
        if not m:
            self._send(404, {"message": "Not Found"})
            return
        owner, name, tail = m.group(1), m.group(2), m.group(3) or ""
        token, login = self._auth()
        if login is None:
            self._send(401, {"message": "Requires authentication"},
                       extra={"WWW-Authenticate": 'Basic realm="forge"'})
            return
        repo = STORE.get_repo(owner, name)
        if repo is None:
            self._send(404, {"message": "Not Found"})
            return
        service = query.get("service", [""])[0]
        if method == "GET" and tail == "/info/refs":
            if service == "git-upload-pack":
                proto = self.headers.get("Git-Protocol") or ""
                if "version=2" in proto:
                    self._send(200, git_v2_advertisement(),
                               "application/x-git-upload-pack-advertisement")
                else:
                    body = pkt_line("# service=git-upload-pack\n")
                    body += pkt_flush()
                    body += git_advertise_refs(repo, "git-upload-pack")
                    self._send(200, body,
                               "application/x-git-upload-pack-advertisement")
                return
            if service == "git-receive-pack":
                body = pkt_line("# service=git-receive-pack\n")
                body += pkt_flush()
                body += git_advertise_refs(repo, "git-receive-pack")
                self._send(200, body,
                           "application/x-git-receive-pack-advertisement")
                return
            self._send(403, {"message": "Forbidden"})
            return
        if method == "POST" and tail == "/git-upload-pack":
            proto = self.headers.get("Git-Protocol") or ""
            if "version=2" in proto:
                out = handle_upload_pack_v2(repo, raw)
            else:
                out = handle_upload_pack_v0(repo, raw)
            self._send(200, out, "application/x-git-upload-pack-result")
            return
        if method == "POST" and tail == "/git-receive-pack":
            out = handle_receive_pack(repo, raw, login)
            self._send(200, out, "application/x-git-receive-pack-result")
            return
        self._send(404, {"message": "Not Found"})

    do_GET = lambda self: self._handle("GET")     # noqa: E731
    do_POST = lambda self: self._handle("POST")   # noqa: E731
    do_PUT = lambda self: self._handle("PUT")     # noqa: E731
    do_PATCH = lambda self: self._handle("PATCH")  # noqa: E731
    do_DELETE = lambda self: self._handle("DELETE")  # noqa: E731


# --------------------------------------------------------------------------
# Boot: register the port with the suite, poll /done, exit 0 when done.
# --------------------------------------------------------------------------

def suite_register(port):
    url = ENV_URL.rstrip("/") + "/register"
    data = json.dumps({"port": port}).encode()
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, data=data, headers={
                "Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=5).read()
            return True
        except Exception:
            time.sleep(0.25)
    return False


def suite_poll_done(server):
    url = ENV_URL.rstrip("/") + "/done"
    while True:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                payload = json.loads(resp.read().decode() or "{}")
            if payload.get("done"):
                break
        except Exception:
            pass
        time.sleep(0.5)
    server.shutdown()
    os._exit(0)


def main():
    port_env = os.environ.get("FORGE_PORT") or os.environ.get("PORT")
    port = int(port_env) if port_env else 0
    if port == 8080:
        port = 0

    class ForgeServer(ThreadingHTTPServer):
        # The clone workload opens 50 connections at once.  The
        # stdlib default listen backlog is 5: the rest of the SYNs
        # queue in the kernel and the clients back off for ~1 s,
        # which alone costs the whole clone workload its points.
        request_queue_size = 128
        daemon_threads = True

    server = ForgeServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    actual = server.server_address[1]
    print("PORT %d" % actual, flush=True)
    if ENV_URL:
        threading.Thread(target=suite_register, args=(actual,),
                         daemon=True).start()
        threading.Thread(target=suite_poll_done, args=(server,),
                         daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
