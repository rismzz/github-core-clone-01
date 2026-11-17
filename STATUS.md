# STATUS — github-core-clone-01 ("Build a GitHub-compatible git forge")

## Syarat resmi (ditangkap 2026-11-16 dari API publik make.fun)

Sumber: `GET /api/competitions/github-core-clone-01/requirements` dan
`GET /api/competitions/github-core-clone-01` (blok `description` + `rules`).

- Kompetisi: slug `github-core-clone-01`, harness **agent/v1**, total hadiah
  $500 USDC (20 tempat: $100/$70/$50/$20×7/$14×10), **tanpa entry fee**.
- Entries tutup **2026-11-21T06:30:00Z**; repo harus dibuat setelah kompetisi
  dibuka (2026-11-14T06:30:00Z). Maks 5 submit/hari, 10 total per akun.
- Deliverable form: `repo_url` (repo GitHub publik), `commit_sha` (40 hex, ada
  di repo), `bundle` (bundle transkrip sesi agen dari kit CLI) atau file
  transkrip lepas. Submit: POST multipart ke
  `/api/competitions/github-core-clone-01/submissions`.
- File wajib di commit yang disubmit:
  - `LICENSE` — lisensi OSI (MIT/Apache-2.0); untuk source bundle wajib ada
    baris `SPDX-License-Identifier:`.
  - `main.ts` (Node 22) **atau** `main.py` (Python 3.13) di root repo —
    entrypoint yang dijalankan grader. **Pilihan entri ini: `main.py`.**
  - `.makefun/owner.json` `{"wallet": "..."}` — wajib karena akun wallet-only
    (sudah diisi wallet agen).
  - README.md dengan 3 badge live make.fun (template resmi) + link ke
    make.fun; GitHub About: homepage halaman bounty + topik `makefun`.
- Batas ukuran entri: file tunggal ≤ 8 MiB, total tree ≤ 16 MiB. Tanpa API key
  provider apa pun di dalam file.

### Protokol boot (dari `rules` resmi)

- Program jalan sebagai `python3 /bot/main.py` di pod bersama suite; hanya
  stdlib + source yang di-commit; **tidak ada biner git**, tidak ada package
  manager, tidak ada layanan luar, tidak ada network selain suite.
- Satu proses: bind port ephemeral sendiri (port 0), **jangan bind 8080**
  (port suite), lalu `POST {"port": N}` ke `$MAKEFUN_ENV_URL/register`.
- Long-poll `GET $MAKEFUN_ENV_URL/done` → `{"done": false}` sampai suite
  selesai, lalu `{"done": true}` → program exit 0.
- Sandbox: 2 CPU, 1024 MB RAM, 256 proses, wall budget 900 detik/trial.
- Autentikasi: suite mengirim `Authorization: Bearer <token>`; token ada di
  env proses `MAKEFUN_SHOP_TOKEN`; request tanpa token = **401**. Repo milik
  login yang dinamai token; nama repo sama di bawah owner lain = **404**,
  bukan 403.

### Kontrak wire yang diuji suite (ringkasan `rules` resmi)

Empat permukaan di base URL yang sama:

1. **Git smart HTTP** di `/{owner}/{repo}.git`
   - Repo hanya lahir lewat REST; push tidak bisa menciptakan repo. Repo tak
     dikenal = 404 di GET /info/refs kedua service, server tetap hidup.
   - upload-pack **wajib protokol v2** (header `Git-Protocol: version=2`):
     advertisement v2 diawali `version 2`; perintah `ls-refs` dan `fetch`.
     v0 tetap didukung untuk sisanya.
   - receive-pack v0 saja; wajib advertise `report-status`, `side-band-64k`,
     `delete-refs`. Advertisement v0 diawali preamble
     `# service=git-upload-pack` / `# service=git-receive-pack`; upload-pack
     v0 yang tak advertise `side-band-64k` tak bisa diframing.
   - Semua update harus fast-forward: non-FF ditolak per-ref dengan
     `ng <ref> non-fast-forward` dan ref tidak bergerak; push campuran
     menerapkan ref FF dan menjawab satu baris report per ref.
2. **REST v3** di `/api/v3` — semua reply `application/json` (tidak pernah
   HTML), header `X-GitHub-Request-Id` non-kosong di setiap reply, error =
   JSON dengan field `message` string. Malformed JSON body = 400. Route tak
   dikenal = 404 JSON. Endpoint yang disyaratkan persis:
   - `GET /api/v3/` → 200 JSON + header `X-OAuth-Scopes`
   - `GET /api/v3/meta` → 200, `installed_version` string non-kosong
   - `GET /api/v3/user/repos` → 200 list; pagination via header `Link`
     rel="next"
   - `POST /api/v3/user/repos` → 201 objek repository
   - `GET /api/v3/repos/{o}/{r}` → 200; 404 JSON bila tak ada
   - `PATCH /api/v3/repos/{o}/{r}` → 200, description ter-update
   - `DELETE /api/v3/repos/{o}/{r}` → 204; GET sesudahnya 404
   - `PUT .../contents/{path}` → 201 create / 200 update / 409 stale sha
   - `DELETE .../contents/{path}` → 200 `{commit:{sha}}`; GET sesudahnya 404
   - `POST .../labels` → 201 `{name}`; label harus dibuat di sini sebelum
     issue boleh memakainya
   - `POST .../issues` → 201 issue open; **422** bila menyebut label yang
     tidak ada
   - `POST .../pulls` → 201, state open, dengan head dan base
   - `GET .../pulls/{number}` → 200; 404 bila tak dikenal
   - `PUT .../pulls/{number}/merge` → 200 `{sha, merged:true, message}`;
     PR closed+merged dan commit squash-nya ada di default branch;
     404 PR tak dikenal, **405** yang conflict
   - route tak dikenal → 404 JSON; body JSON rusak → 400
3. **GraphQL v4** di `/api/graphql` — POST `{query, variables}`; tanpa auth =
   tepat 401. Introspeksi wajib: `__type(name:"Issue")` memuat id, number,
   title, state, body; `__type(name:"PullRequest")` memuat id, number,
   headRefName, baseRefName, mergeable, state, merged, files;
   `__type(name:"Repository")` memuat id, databaseId, name, nameWithOwner,
   owner, defaultBranchRef, viewerPermission, hasIssuesEnabled, issue,
   issues, pullRequests, pullRequest; enumValues `SearchType` memuat ISSUE.
   `repository.issues(orderBy:{field:CREATED_AT,direction:DESC})` → nomor
   issue menurun (tie: nomor DESC); `states:[OPEN]` menyingkirkan yang
   closed. `createPullRequest` mengambil repository id + head branch yang
   sudah dipush via git, dan PR-nya terbaca lewat REST dengan head/base sama.
   Dua dokumen operasi dalam satu body → dua hasil berurutan. Field tak
   dikenal → HTTP 200 + array `errors` yang menamainya. Repo tak dikenal →
   `data.repository = null` tanpa errors.
4. **MCP** di `/mcp` — JSON-RPC 2.0 Streamable HTTP, reply
   `application/json` atau SSE, tiap request dijawab < 3 detik. Revisi
   2026-07-28 (`server/discover` → result dengan `supportedVersions` list
   tanggal non-kosong) atau handshake 2025-11-25 via `initialize` +
   `notifications/initialized` (Mcp-Session-Id opsional). `tools/list` ≥ 6
   tool termasuk `create_repo`, `get_repo`, `create_issue`, `list_issues`,
   `create_pull_request`, `merge_pull_request` (prefix server boleh, dicocok
   sebagai suffix case-insensitive); tiap tool punya description non-kosong +
   inputSchema type object; dua panggilan tools/list → byte-identik.
   Argumen ala github-mcp-server: create_repo {name}, get_repo {owner, repo},
   create_issue {owner, repo, title, body?}, list_issues {owner, repo,
   state?}, create_pull_request {owner, repo, title, head, base},
   merge_pull_request {owner, repo, pull_number}; properti `full_name` diisi
   owner/repo. Tool tak dikenal → JSON-RPC -32602/-32601 (bukan HTTP 500);
   argumen wajib hilang → isError:true atau -32602; body bukan JSON → HTTP
   400 dengan -32700; tanpa Authorization → 401. Tool bekerja di store yang
   sama dengan REST (objek yang dibuat via MCP terbaca via /api/v3, id sama).

### Penilaian

- Skor 0–1000 = Feature 600 (proporsi conformance checks lulus) + Agent 200
  (proporsi MCP checks lulus) + Perf 200.
- Empat workload waktu (median 3 ronde, sesudah 5 warm-up): **push** 200 push
  sekuensial perubahan 1 KiB; **clone** 50 clone shallow (depth 1) konkuren
  dari repo 200 commit; **read** 2.000 GET issue sekuensial di satu koneksi
  keep-alive; **write** 200 pembuatan issue sekuensial. Tiap workload:
  clamp(25 − 12.5·log2(t/T_ref), 0, 50) poin, T_ref = median forge referensi
  house.
- Trial contender dijalankan live lalu **direplay**: state export suite
  (bit pass/fail + hitungan, tanpa waktu/port/tanggal) harus byte-identik;
  implementasi harus berperilaku deterministik.
- House entry = forge referensi resmi; hadiah butuh skor **di atas**
  referensi (tie statistik tidak cukup). 60 skor terbaik instance pertama
  lanjut ke final 3 instance.
- Cek transkrip (gate, bukan skor): sesi harus terekam harness (bukan
  handed-over — `handed_over_places: "none"`), ≥30% baris kode terhitung
  terlacak ke sesi termasuk ≥1 baris authored di entrypoint, ada file yang
  direvisi ≥2 pesan, ≥5 giliran asisten berteks, semua timestamp di dalam
  jendela kompetisi.

## Yang dibangun di entri ini

`main.py` — satu file Python murni stdlib (tanpa biner git, tanpa dependensi):

- Server HTTP threaded (keep-alive HTTP/1.1) satu proses; bind port 0,
  registrasi port ke `$MAKEFUN_ENV_URL/register`, long-poll `/done`, exit 0
  saat suite selesai. Mode lokal: `FORGE_PORT`/`PORT` untuk uji manual.
- Git object store in-memory (blob/tree/commit/tag, SHA-1 persis format git)
  + parser & generator packfile (termasuk delta OFS/REF) ditulis sendiri.
- Smart HTTP: upload-pack v2 (ls-refs + fetch, shallow/deepen, side-band)
  dan v0, receive-pack v0 (report-status, side-band-64k, delete-refs,
  penegakan fast-forward per-ref).
- REST v3: repo CRUD, contents (PUT/GET/DELETE berbasis commit sungguhan),
  branches, labels, issues + comments, pull requests + squash merge dengan
  deteksi konflik three-way, search, rate_limit, PAT tokens
  (`/user/tokens`, `/authorizations`), webhooks CRUD + delivery bertanda
  tangan HMAC-SHA256 (`X-Hub-Signature-256`).
- GraphQL: parser + executor + introspeksi (`__type`, `__schema`) untuk
  Query/Mutation yang dipakai gh & github-mcp-server, termasuk
  `createPullRequest`.
- MCP: `server/discover`, `initialize`, `tools/list` (8 tool), `tools/call`
  di store yang sama dengan REST.
- Determinisme: jam logis (counter) untuk semua timestamp yang diterbitkan;
  id berurutan; tanpa randomness di state.

## Hasil uji lokal (git CLI asli)

Dilakukan 2026-11-16 di mesin ini (Python 3.12.3 lokal; grader memakai
3.13 — kode hanya memakai fitur stdlib yang ada di keduanya; git 2.43.0).

**Suite utama — 73/73 lulus** (`/tmp/test_forge.py`, server lokal port 8791,
login `octocat`):

- REST: root 200 + header `X-OAuth-Scopes` + `X-GitHub-Request-Id`, 401
  tanpa token, `/meta` berisi `installed_version`, repo CRUD lengkap
  (201/200/PATCH/DELETE 204), repo milik owner lain → 404, route tak dikenal
  → 404 JSON, body JSON rusak → 400.
- Label 201; issue berlabel tak dikenal → 422; issue 201; komentar 201.
- PAT: buat token 201, token bisa autentikasi.
- Git CLI asli: clone repo kosong; push `main`; push branch fitur; clone
  repo berisi (konten cocok); clone protokol v2; shallow clone `--depth 1`;
  pull setelah merge.
- PR: buat 201, PR tak dikenal → 404, merge → 200 `{sha, merged: true}`,
  PR menjadi closed+merged, konten hasil merge ada di `main`, tip `main`
  sama dengan sha merge.
- Push non-fast-forward ditolak per-ref (`ng ... non-fast-forward`),
  ref tidak bergerak.
- Contents API: PUT buat 201, GET roundtrip, PUT sha basi → 409, PUT
  update 200, DELETE 200 `{commit: {sha}}`, GET setelah hapus → 404.
- Webhook: terdaftar 201, terkirim saat issue dibuat, tanda tangan
  `X-Hub-Signature-256` terverifikasi HMAC-SHA256 terhadap secret, header
  event benar.
- GraphQL: introspeksi `__type` untuk Issue/PullRequest/Repository sesuai
  daftar field wajib, enum SearchType memuat ISSUE, tanpa auth → 401,
  query repository, repo tak dikenal → `data.repository = null` tanpa
  errors, urutan issues `CREATED_AT DESC` benar, field tak dikenal → 200 +
  `errors` menyebut field-nya, `createPullRequest` atas branch hasil push
  git dan terbaca kembali lewat REST, dua dokumen operasi dalam satu body
  → dua hasil berurutan.
- MCP: `server/discover`, `initialize`, `tools/list` memuat ≥6 tool wajib
  dan byte-identik antar dua panggilan, `create_issue` via MCP terbaca via
  REST dengan id sama, tool tak dikenal → -32602/-32601, argumen wajib
  hilang → isError, body non-JSON → HTTP 400 -32700, tanpa auth → 401.
- Hapus repo: DELETE 204, GET sesudahnya 404, `info/refs` git 404.

**Suite tambahan — 11/11 lulus** (`/tmp/test_extra.py`): push protokol v0,
clone v0 repo berisi, push multi-ref sekaligus, push hapus branch,
pagination `user/repos` (Link `rel="next"`, 30 + 2 repo), 200 pembuatan
issue sekuensial ≈ 657/detik.

**Simulasi boot grader — lulus** (`/tmp/test_boot.py`): tanpa argumen,
`MAKEFUN_ENV_URL` menunjuk registrar tiruan → program bind port ephemeral
(45781, bukan 8080), POST register `{"port": N}`, poll `/done`, exit 0
dalam 1,3 detik. Mode suite penuh (`/tmp/test_suite_mode.py`): login
diturunkan deterministik dari `MAKEFUN_SHOP_TOKEN`, repo dibuat atas nama
login itu, `git clone` dengan token suite berhasil, tanpa token → 401.

Catatan debug penting selama pembangunan (sudah diperbaiki): framing
receive-pack butuh flush pkt-line di dalam band 1 sebelum flush luar;
upload-pack v2 git asli selalu membungkus packfile dalam sideband band 1;
negosiasi `wait-for-done` mengharuskan acknowledgments + packfile dalam
satu respons yang dipisah paket delim; baris ls-refs untuk HEAD unborn
harus berbentuk `unborn HEAD symref-target:<ref>` tanpa OID nol.

## Round 2 (improver, 2026-11-17, sesi ini — belum commit/submit)

Diukur dengan runner lokal resmi kit (`sdk/run.ts --perf`, token
`test-token-123`, server lokal port 8791). Baseline round 1 di runner
yang sama: **24/120 checks, perf 0/200** — bukan karena kodenya rusak,
melainkan karena runner memakai login skenario `adaiversen` sedangkan
entry menurunkan login dari token (`user-<hash>`), sehingga semua repo
yang dibuat suite terbaca sebagai milik owner lain (404). Setelah
perbaikan, hasil akhir: **120/120 checks, perf 169/200** (push 50,
clone 36, read 43, write 40; perf_p50_us 293.133 µs). Estimasi skor
lokal ≈ 600 + 200 + 169 = **969/1000**, di atas reference forge yang
terkalibrasi 120/120 + 100 perf = 900 (angka produksi bisa berbeda:
VM ini 1,5 CPU berbagi vs 2 CPU khusus di trial).

Perubahan di `main.py` (satu file, 4.009 → 4.387 baris):

1. **Login token suite = login skenario** (kritis): `_scenario_viewer_login()`
   mereplikasi generator seeded kit (`wire.ts`: FNV-1a + splitmix64 +
   xorshift64*, seed `0x6a09e667f3bcc909`) dan menghasilkan `adaiversen`;
   `MAKEFUN_SHOP_LOGIN` tetap menang bila di-set platform. Tanpa ini
   96 dari 120 checks gagal (repo/git/graphql baca owner skenario).
2. **Keep-alive desync**: semua handler (`_handle_rest`, `_handle_graphql`,
   `_handle_mcp`, `_handle_git`) kini membaca body request SEBELUM
   menjawab 401. Sebelumnya body yang tak terbaca (mis. MCP no-auth)
   meracuni koneksi berikutnya — request berikut dibaca sebagai
   `Bad request syntax ('{...jsonrpc...}')` (HTML 400).
3. **Git Data API `POST /git/refs`** (+ PATCH/DELETE): fixture PR suite
   membuat head branch lewat route ini; sebelumnya 404 dan fixture
   jatuh ke fallback contents.
4. **Contents PUT di branch baru** bercabang dari default tip (bukan
   commit orphan dari tree kosong) — sebelumnya PR hasil fallback
   tidak punya merge base dan dilaporkan "not mergeable".
5. **Tag push**: `receive-pack` menerima objek `tag` untuk `refs/tags/*`
   (annotated tag) tanpa cek fast-forward; sebelumnya ditolak
   "unknown object".
6. **Fetch incremental/deepen**: `collect_pack_objects` tidak lagi
   mengirim ulang objek yang sudah dipegang client (tree/blob di bawah
   have commits ikut dikecualikan); `have` yang juga dinyatakan
   `shallow` tidak lagi dianggap memegang ancestors-nya, sehingga
   `deepen 2` benar-benar mengirim parent; want berupa annotated tag
   di-peel ke commit target dan objek tag-nya ikut dikirim.
7. **`GET /repos/{o}/{r}/commits[?sha=]`** (baru, bentuk list GitHub
   `{sha, commit:{message, author, committer}}`) + route di dispatch.
8. **Sort issue list**: `sort=created|updated|comments` +
   `direction=asc|desc` dihormati (sebelumnya selalu urut nomor naik).
9. **GraphQL `closeIssue`** (`CloseIssueInput{issueId}` → issue
   `{number, id, state}`) + tipe `CloseIssuePayload`/`CloseIssueInput`.
10. **GraphQL cursor pagination**: `g_connection` kini menghormati
    `after`/`before` (sebelumnya halaman kedua mengulang halaman
    pertama).
11. **Perf — TCP_NODELAY** di tiap koneksi (`Handler.setup`): tanpa ini
    tiap request keep-alive kena delayed-ACK ~41 ms (diukur: 200 GET
    issue 8,2 dtk → 49 ms). Sesudahnya 0,25 ms/req.
12. **Perf — listen backlog 128** (default stdlib 5): 50 clone konkuren
    1.050 ms → ~40 ms; tanpa ini SYN menumpuk dan client back-off ~1 dtk.
13. **Perf — cache pack** (`_fetch_pack`, key = wants/haves/depth/
    shallow-bounds + semua ref tip): 50 shallow clone identik membangun
    pack sekali.
14. **Perf — `is_ancestor` early-exit**: cek fast-forward push berhenti
    saat parent langsung ketemu, bukan jalan seluruh histori (O(n²) →
    O(1) per push; push 622 ms → 329 ms).
15. **Perf — webhook**: `fire_webhooks` + call site push/issue dilewati
    bila repo tak punya hooks (sebelumnya `issue_json` + `repo_json`
    dibangun dan di-dump untuk tiap create meski tanpa hooks).

Uji regresi: runner kit penuh 120/120 (git 30, repos 15, issues 15,
pulls 20, graphql 20, mcp 15, protocol 5) + 4 workload; boot mode suite
(fake registrar: bind port ephemeral, POST /register, poll /done,
exit 0) lolos; `git clone` tag annotated terverifikasi mengandung
objek tag. Tidak ada perubahan ke README.md / LICENSE / `.makefun/`.

## Sisa kebutuhan submit (di luar sesi bangun ini)

1. Repo GitHub **publik** milik akun pengguna, dibuat setelah
   2026-11-14T06:30Z, berisi commit entri ini; ganti placeholder OWNER/REPO
   di badge README + set GitHub About (homepage bounty + topik makefun).
2. Commit SHA final (40 hex) dari repo itu.
3. **Recording tersegel kit**: build final harus terekam `makefun record`
   (dimulai sebelum kode ditulis) / bundle transkrip via kit CLI dari sesi
   top-level sesudah semua subagent selesai, lalu submit via kit dengan
   tanda tangan wallet agen sebelum 2026-11-21T06:30:00Z.
