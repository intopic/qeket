# Canary Upgrade Gate

MVP lokal për përditësime të kontrolluara të programeve Solana. Një komandë ndërton dy versione të një programi Rust, rindërton kandidatin nga i njëjti commit dhe provon përditësimin me loader-in real të Solana brenda LiteSVM. Të gjithë çelësat e votimit krijohen në memorie për atë ekzekutim.

## Ekzekutimi

Në GitHub hap **Actions → Canary MVP**. Çdo push ose pull request nis testet në Ubuntu. Mund t'i nisësh edhe nga **Run workflow**. Job-i `verify` ndërton me platform-tools v1.58 të kontrolluar me SHA-256, ekzekuton testet Rust dhe provën në LiteSVM, pastaj ruan binarët dhe arkivin si artifact të commit-it.

Te **Actions → Canary MVP → Summary**, seksioni **Canary load test** jep rezultatin e replay-it me 1,000 transaksione sintetike, përfshirë 100 raste v0 me ALT dhe CPI, si edhe refuzimin e kandidatit me gabim në transaksionin 901. Artefakti `canary-mvp-<commit>` përmban `target/mvp/evidence/load-test.json`, `report.json` dhe `archive.json`. Matjet përfshijnë kohën e ndërtimit, pesë ekzekutime të replay-it, CPU, RAM dhe madhësinë e arkivit. Prova e pranimit simulon 24 kërkesa në të njëjtën kohë nga tre klientë mbi një bazë SQLite. Ajo shfaqet te logu **Check Rust workspace**. Lokalisht, `python scripts/mvp.py --load-test` ekzekuton të njëjtën provë të replay-it.

Matjet e të njëjtit benchmark në laboratorin Fly.io Sprites ruhen në [sprite-594c247.json](docs/test-results/sprite-594c247.json). Benchmark-u përdor transaksionë sintetike dhe një runner të vetëm për mjedis.

Job-i `Devnet RPC compatibility` shkarkon po atë artifact, kontrollon identitetin e Devnet-it dhe loader-in v3, dhe kërkon tarifën për mesazhin e votës së testuar me një blockhash të finalizuar. Nëse programi ekziston në adresën e konfiguruar, krahason edhe bajtët e tij me binarin e CI-së. `devnet-report.json` tregon slotin, tarifën dhe nëse kontrata është publikuar. Ky hap është vetëm për lexim dhe funksionon pa kredenciale.

Nga rrënja e projektit, në këtë kompjuter:

```sh
python scripts/mvp.py
```

Rezultati ruhet te `target/mvp/evidence/report.json`. Arkivi `archive.json` pranë raportit përmban transaksionet e nënshkruara, llogaritë fillestare, orët për secilin slot dhe buffer-in. Komanda e rilexon arkivin nga disku dhe riprodhon rrënjën Merkle me CLI-në `canary-gate verify`. `python scripts/mvp.py --build-only` kontrollon vetëm ndërtimet.

Në një kompjuter të ri duhen Python 3, Git, Rust me Cargo, platform-tools v1.58 nëpërmjet `cargo-build-sbf` ose një toolchain Rust Solana të lidhur me rustup. LiteSVM kërkon edhe OpenSSL për hostin. Në Windows mbështetet një instalim zhvillimi me `OPENSSL_DIR`, bibliotekat DLL në `PATH` dhe `OPENSSL_NO_VENDOR=1`. Në këtë workspace skripti gjen automatikisht kopjen lokale të OpenSSL në `target/mvp/openssl/Library`. Për të përgatitur cache-in e Cargo-s në një instalim të freskët:

```sh
cargo fetch --locked
cargo fetch --locked --manifest-path examples/counter/Cargo.toml
```

## Çfarë provon MVP-ja

1. Skripti regjistron shembullin e projektit në një depo Git lokale brenda `target/mvp`. Dy dosje burimore marrin skedarët nga commit-i i regjistruar. Ndërtimet e kandidatit përdorin target-e të veçanta, të njëjtin Cargo.lock dhe Cargo offline. Krahasohen të gjithë bajtët.
2. Kontrata Anchor verifikon autoritetin e qeverisjes. Loader-i i kalon PDA-së së kontratës autoritetin e programit dhe të buffer-it. Propozimi fikson hash-in e kandidatit, commit-in, versionin bazë dhe dritaren e replay-it.
3. Një VM ekzekuton 252 transaksione lokale të nënshkruara mbi programin bazë. Arkivi ruan pozicionin e secilit në bllok. Secili nga dy verifikuesit krahason ndërtimin përkatës me buffer-in dhe riekzekuton të njëjtat transaksione në dy VM me gjendje fillestare të njëjtë. Të dy modalitetet kontrollojnë suksesin, kufirin CU dhe manifestin e transaksioneve. Çdo slot i shembullit përfshin një transaksion v0 me ComputeBudget dhe një router që thërret programin përmes CPI, duke përdorur një ALT për llogarinë e shkrueshme dhe programin e synuar.
4. Dy nënshkrime Ed25519 mbi të njëjtën rrënjë plotësojnë kuorumin 2/3. Prova kontrollon refuzimin e votës së dyfishtë, ekzekutimin para kuorumit dhe ekzekutimin gjatë timelock-ut. Instruksioni kompakt lexon direkt mesazhin prej 186 bajtësh nga precompile-i i mëparshëm. Forma origjinale që e përsërit payload-in mbetet e mbështetur.
5. Pas timelock-ut, kontrata thërret loader-in me PDA-në. Prova kontrollon bajtët e vendosur në ProgramData, ekzekuton versionin e ri dhe lexon vlerën që ai shkroi. Në një kopje tjetër të VM-së provohen rotacioni i politikës, anulimi, mbyllja e propozimit dhe rikuperimi nga autoriteti emergjent.

## Matjet në LiteSVM

| Veprimi | Madhësia e transaksionit | Compute units |
| --- | ---: | ---: |
| Vota e parë kompakte | 610 B | 7 865 |
| Vota që plotëson kuorumin | 610 B | 7 893 |
| Ekzekutimi i përditësimit | 442 B | 16 458 |

Tabela ruan matjet e MVP-së fillestare. Matjet e çdo commit-i ruhen në artifact-in e CI-së te `report.json`. Komanda dështon nëse një nga votat ose ekzekutimi arrin 650 bajtë ose 25 000 CU.

## Kodi dhe provat

- `programs/canary_upgrade_gate` përmban kontratën. Ndryshimet e politikës përdorin një PDA të përkohshme dhe timelock-un aktual. Rotacioni ndryshon gjeneratën e nonce-it dhe i zhvlerëson propozimet e vjetra. Ato mund të anulohen dhe mbyllen.
- `crates/canary-types` përcakton mesazhin Borsh. `crates/svm-canary-runner` përmban verifikuesin, CLI-në dhe skenarin e plotë LiteSVM.
- `examples/counter` është programi i provës. Rritjet pozitive prodhojnë të njëjtin rezultat në V1 dhe V2. Për hyrjen zero V2 vendos një rritje minimale prej një njësie. Kjo ushtron të dyja politikat e krahasimit.
- `cargo test --workspace --locked` ekzekuton testet Rust. `cargo fmt --all --check` kontrollon formatimin. Në Windows testet e workspace-it përdorin të njëjtat variabla OpenSSL si CLI-ja.
- `.github/workflows/local-mvp.yml` përmban hapat e CI dhe të provës RPC në Solana Devnet.

## Kufijtë e provës lokale

Arkivi v2 ruan transaksionet e nënshkruara në formatin binar Solana të koduar në hex. CLI lexon edhe arkivat v1 JSON me transaksione legacy dhe politikën `StrictEquality`. Snapshot-i përmban llogaritë ALT, programet e varura dhe ProgramData për programet e loader-it v3. Llogaritë që nuk ekzistonin në fillim shënohen te `absent_accounts`. Tabelat në çaktivizim kërkojnë `slot_hashes` për çdo slot. LiteSVM kontrollon nënshkrimet dhe përdor orën e arkivit. Freskia e blockhash-it dhe origjina e listës së transaksioneve i besohen regjistruesit.

`Policy.replay_mode` zgjedh mes `StrictEquality`, që krahason edhe të dhënat e kthyera dhe çdo llogari të shkrueshme, dhe `NoRevertAndCuBound`, që lejon ndryshime të qëllimshme. Të dyja kërkojnë transaksione bazë të suksesshme, ekzekutim të suksesshëm të kandidatit dhe respektim të kufirit CU për çdo transaksion. Në të dyja rastet rrënja përmbledh gjendjen e kandidatit pas çdo transaksioni sipas slotit, indeksit dhe çelësit të llogarisë. Modaliteti lidhet me nënshkrimin përmes domain-it të votës.

## Rikuperimi dhe rotacioni

- Qeverisja thërret `reclaim_buffer` pas anulimit ose kalimit të afatit të ruajtur në propozim. Loader-i kthen rent-in te `spill_account`. Pastaj `close_proposal` çliron propozimin dhe rezervimin ekskluziv të buffer-it.
- Një buffer që iu dorëzua gate-it, por nuk u propozua kurrë, p.sh. pas një CI-je që dështoi ose një propozimi në Squads që nuk u miratua, kthen rent-in me `reclaim_orphan_buffer`. Instruksionin e nënshkruan qeverisja, dhe ai pranohet vetëm kur buffer-i nuk ka rezervim, pra kur nuk i përket asnjë propozimi.
- `propose_authority_change` regjistron rolin, adresën e re dhe afatin. Pas timelock-ut, qeverisja dhe autoriteti pritës nënshkruajnë `execute_authority_change` për rotacionin e qeverisjes ose emergjencës. Qeverisja mund ta anulojë kërkesën me `cancel_authority_change`.
- Për transferimin e autoritetit të programit, qeverisja përdor rolin `ProgramUpgrade` dhe thërret `transfer_program_authority` pas afatit. Autoriteti emergjent mund ta thërrasë menjëherë. Marrësi nënshkruan në të dyja rrugët. Loader-i ekzekuton `SetAuthorityChecked`. Transferimi zhvlerëson gjeneratën e mëparshme dhe qeverisja mund të mbyllë kërkesën e përdorur për të marrë rent-in.
- Konfigurimi on-chain përdor skemën v2 me llogari të inicializuara rishtas. Migrimi në vend i llogarive on-chain v1 kërkon një migrim të veçantë.

Dy verifikuesit janë simulime në të njëjtin kompjuter. Prova përdor çelësa të veçantë lokalë për qeverisjen dhe emergjencën. Për prodhim mbeten operatorë të pavarur, integrimi Squads/Realms dhe KMS/HSM, izolimi hermetik i build-it, marrja e të dhënave të finalizuara nga Yellowstone, validimi i snapshot-eve historike kundrejt të dhënave të finalizuara, fuzzing me Trident, prova afatgjatë në Devnet dhe auditimi i jashtëm.

## Cikli live në Devnet

Te **Settings → Secrets and variables → Actions** vendos:

| Secret | Vlera |
| --- | --- |
| `RPC_URL` | HTTPS URL e Helius për Devnet, përfshirë API key |
| `DEVNET_PAYER` | Array JSON i 64 bajtëve për një portofol të dedikuar, të financuar me SOL testues për rent dhe tarifa |
| `GATE_PROGRAM_KEYPAIR` (opsional) | Array JSON i keypair-it të programit. Pa të, workflow derivon një adresë të qëndrueshme Devnet nga payer-i dhe ndërton gate-in dhe runner-in për atë adresë. |

Nis **Actions → Live Devnet counter → Run workflow** në `main`. Një push provon ndërtimin offline dhe gjithë ciklin në një validator të përkohshëm, me dy worker-a të pavarur në të njëjtin runner. Vetëm nisja manuale dërgon transaksione në Devnet. Aty votat i japin job-et `verifier-a` dhe `verifier-b`, secili në runner-in e vet. Secili mban vetëm çelësin e vet të votës, kap gjendjen live në pranim, ndërton kandidatin nga commit-i i propozimit, riekzekuton dritaren dhe voton. Job-i `cycle` nuk voton. Ai pret që të dy worker-at të raportojnë `Verifier ready`, pastaj propozon, dërgon trafikun, pret kuorumin dhe kontrollon që `diff_root` i votuar të jetë i njëjtë me replay-in e snapshot-it të kontrolluar. Pas ekzekutimit, secili worker regjistron `executed`. Çdo nisje e re krijon target të ri dhe konsumon rent Devnet. Nëse një nisje dështon pasi ka inicializuar gate-in, artifact-i përmban hapat e finalizuar deri aty. Ruaj payer-in dhe run ID për rikuperimin e autoriteteve.

Artifact-i `devnet-live-<run_id>` përmban `transactions.md` dhe arkivin e replay-it. Provën mund ta nisësh me 50–100 transaksione të counter-it, secila me ComputeBudget. Snapshot-i i shembullit merret nga RPC para propozimit për counter-in dhe fee payer-in e izoluar. Workflow kërkon pikërisht transaksionet e pritura në dritaren e finalizuar dhe rikonfirmon bytecode-in e instaluar. Counter-i nuk lexon sysvar Clock. Për programe arbitrare, komanda `python scripts/live_capture.py capture SNAPSHOT.json ARCHIVE.json --max-replay-txs 500` kërkon gjendjen historike të fillimit të dritares, përfshirë ALT, ProgramData dhe sysvars përkatëse. `minContextSlot` nuk rikthen gjendjen historike. CLI ekzistuese `canary-gate verify` riekzekuton arkivin. Në provën Devnet, ndërtimin e kandidatit dhe votimin i bëjnë worker-at e verifikuesve, jo skripti i ciklit. Artifact-et `devnet-verifier-a-<run_id>` dhe `devnet-verifier-b-<run_id>` përmbajnë logun, SQLite-in, arkivin dhe `evidence.json` të secilit.


### Whitelist dhe kuota e klientëve

Vendos `CANARY_CUSTOMERS` si Fly secret në secilin verifikues. Formati është një objekt JSON me ID të qëndrueshme të klientëve.

```json
{
  "protocol-a": {
    "targets": ["<program-id>"],
    "active_until": "2027-01-01T00:00:00Z",
    "monthly_proposals": 25
  }
}
```

Zëvendëso `<program-id>` me adresën publike të programit të klientit. Afati është ekskluziv. Kuota mat propozimet unike të pranuara për klientin, brenda muajit kalendarik UTC, dhe ndahet ndërmjet programeve të tij. Një propozim i pranuar ruhet vetëm një herë. Çdo verifikues ruan numërimin dhe pozicionin e leximit në SQLite, në volumin e vet. Politikat e klientëve mirëmbahen në secret dhe hyjnë në fuqi kur publikohet konfigurimi i ri. Për çdo propozim të pranuar, worker-i kap gjendjen live, pret finalizimin e dritares, ndërton kandidatin dhe voton (shih [kapjen live](#kapja-live-e-gjendjes-fillestare)).


| Plani | Abonimi mujor (USD) | Kontrata | Propozime për upgrade në muaj | Transaksione replay për propozim | Për kë është |
| --- | ---: | --- | ---: | ---: | --- |
| **Starter** | **$490** | 1 smart kontratë + Devnet falas | Deri në 5 | Deri në 500 | Startup-e dhe projekte me një program kryesor, p.sh. staking, token ose vault. |
| **Pro** *(Plani i rekomanduar)* | **$1,900** | Deri në 3 smart kontrata + $450/muaj për çdo kontratë shtesë | Deri në 20 | Deri në 2,000 | Protokolle aktive DeFi/DePIN me $5M–$50M TVL dhe zakonisht 2–3 programe (`core`, `router`, `rewards`). Oferta parashikon Squads v4 dhe alerte. |
| **Enterprise** | **Nga $4,500** | Deri në 10 smart kontrata | Pa limit, me cooldown 15–30 min | Deri në 5,000 | Protokolle me $50M+ TVL, ura cross-chain dhe RWA. Oferta parashikon nyje të dedikuar, kuorum 3/5 dhe SLA. |

Tabela përshkruan ofertën komerciale. Devnet falas, kufijtë e kontratave sipas planit, faturimi i kontratave shtesë dhe cooldown-i i paracaktuar i Enterprise mbeten për t'u lidhur me faturimin dhe konfigurimin e klientëve. Integrimi live me Squads v4, alertet, nyjet e dedikuara, kuorumi 3/5 dhe SLA mbeten për t'u verifikuar operacionalisht.

Abonimet B2B faturohen jashtë zinxhirit.

Mund të caktohen `active`, `cooldown_minutes`, `max_upgrades_per_month` dhe `max_replay_txs`. Përditëso `CANARY_CUSTOMERS` ose menaxho skedarin `/data/customers.json` me `python scripts/customer_admin.py --file customers.json set CLIENT --plan starter --target PROGRAM`. Për një klient ekzistues përdor `suspend CLIENT` ose `plan CLIENT --plan pro`. Skedari në volum ka përparësi ndaj secret-it. Një job i pranuar bllokon pranimin e job-eve të tjera të klientit deri në mbyllje. Job-i mbyllet vetë me një rezultat, kur skadon propozimi ose pas një rinisjeje. Operatori mund ta mbyllë edhe me dorë, me `python fly_listener.py --finish PROPOSAL`. Tavani i replay-it ruhet në payload-in e job-it. SQLite zbaton kuotën për një instancë. Për kuotë të përbashkët mes nyjeve, kërkohet ruajtje e përbashkët e vendimeve të pranimit.

### Dy verifikues të pavarur në Fly.io

`deploy/fly/verifier/` publikon dy aplikacione me nga një makinë: `canary-verifier-a` në `fra` dhe `canary-verifier-b` në `iad`. Secila ka `shared-cpu-2x`, 2 GB RAM, 2 GB swap dhe volum `/data` 10 GB. Imazhi përmban `canary-gate`, të ndërtuar me ID-në e gate-it, dhe Docker. Në nisjen e parë, worker-i shkarkon platform-tools v1.58 në volum dhe kontrollon SHA-256. Build-et e kandidatit përdorin të njëjtin kontejner offline si CI-ja: pa rrjet, pa capabilities dhe me burim read-only. Hapat e deploy-it janë te [konfigurimi Fly](deploy/fly/verifier/README.md).

Build-i dhe çelësi i votës janë në të njëjtën makinë. Listener-i ka qasje në Docker që të nisë kontejnerin e build-it, dhe kjo në praktikë është qasje root. Një dalje nga kontejneri do ta zbulonte çelësin. Për këtë duhet një gabim në kernel ose në `runc`, dhe kodin e ndërton commit-i që zgjedh propozuesi. Të dy verifikuesit e ndërtojnë të njëjtin commit, dhe çelësat e tyre vlejnë për të gjithë klientët. Për Devnet ky rrezik pranohet. Para Mainnet rekomandohet që build-i të kalojë në një makinë pa çelës. `CANARY_BUILDER_HOST` e dërgon build-in me SSH te një host i tillë, ku `worker_build.py serve-build` është komanda e vetme e lejuar. Konfigurimi Fly për këtë host ndodhet në commit-in `9238584`.

Gjenero çelësin e votës veçmas për secilin aplikacion, në kompjuterin tënd. Çelësi sekret shkon vetëm te `fly secrets`, ndërsa adresa publike shfaqet në ekran:

```powershell
fly secrets set -a canary-verifier-a CANARY_VERIFIER_KEYPAIR="$(python scripts/solana_tx.py keygen)"
```

Workflow-i **Deploy Fly verifiers** kërkon secret-et e repos `FLY_API_TOKEN_VERIFIER_A` dhe `FLY_API_TOKEN_VERIFIER_B`, nga një token deploy i kufizuar për secilin aplikacion. Nëse aplikacioni nuk ka `RPC_URL`, workflow-i e merr nga secret-i `RPC_URL` i repos. Deploy-i ndalet nëse mungon `CANARY_VERIFIER_KEYPAIR` ose `CANARY_CUSTOMERS`.

`CANARY_CLUSTER` zgjedh rrjetin: `devnet`, që është vlera e paracaktuar, ose `mainnet-beta`. Worker-i nuk niset nëse `RPC_URL` i përgjigjet me genesis të një rrjeti tjetër.

### Adapteri Squads v4

`packages/squads-adapter` ndërton instruksionet e gate-it dhe i mbështjell me `vaultTransactionCreate` nga SDK Squads. Kalo adresën e gate-it të publikuar, adresën e multisig-ut, indeksin e Vault-it dhe të dhënat aktuale të propozimit. Vault-i duhet të jetë autoriteti për veprimet e qeverisjes dhe të ketë SOL për llogaritë e përdorura gjatë inicializimit ose propozimit. Ndiq rrjedhën e zakonshme Squads për krijimin e propozimit, miratimet dhe ekzekutimin. Paketa mbulon çdo instruksion të gate-it që nënshkruan qeverisja ose autoriteti emergjent:

| Veprimi | Funksionet |
| --- | --- |
| Inicializimi dhe propozimi | `initializeGatekeeper`, `proposeBufferUpgrade`, `executeGatedUpgrade` |
| Anulimi dhe mbyllja e propozimit | `cancelProposal`, `reclaimBuffer`, `closeProposal`, `reclaimOrphanBuffer` |
| Ndryshimi i politikës, p.sh. `StrictEquality` ↔ `NoRevertAndCuBound` | `proposeConfigUpdate`, `executeConfigUpdate`, `cancelConfigUpdate` |
| Rotacioni i autoriteteve | `proposeAuthorityChange`, `executeAuthorityChange`, `cancelAuthorityChange` |
| Autoriteti i programit | `proposeProgramAuthorityTransfer`, `transferProgramAuthority` |
| Upgrade emergjent | `emergencyOverrideUpgrade` |

Ndryshimet e politikës dhe të autoriteteve zbatohen pas timelock-ut. Zbatimi i politikës së re zhvlerëson propozimet e hapura. Te `executeAuthorityChange` dhe `transferProgramAuthority` nënshkruan edhe marrësi i ri, sipas mënyrës si ai autoritet nënshkruan. `emergencyOverrideUpgrade` e nënshkruan autoriteti emergjent. Kur ai është Vault Squads, mbështille me `createVaultTransaction` të multisig-ut të tij. Paketa krijon instruksionet dhe payload-in e Vault-it. Testi krahason çdo instruksion me rendin e llogarive te `contexts.rs`. Integrimi me një Vault të publikuar kërkon ende test on-chain.

### CLI `canary` dhe GitHub Action

`canary` është ana e klientit për një upgrade përmes gate-it. Ndodhet te `packages/squads-adapter` (`npm run build`, pastaj `node dist/cli.js`):

| Komanda | Çfarë bën |
| --- | --- |
| `config-address` | Adresa e konfigurimit të gate-it. Asaj i kalohet autoriteti i programit dhe i buffer-it. |
| `window` | Mat trafikun e fundit të programit dhe zgjedh `replay_slots`, që dritarja të arrijë minimumin e gate-it dhe të mbetet nën tavanin e planit (`--max-replay-txs`). |
| `propose` | Kontrollon që buffer-i mban binarin e ndërtuar dhe që autoriteti i tij është gate-i. Pastaj hap në Squads një propozim për `propose_buffer_upgrade`, me hash-in, commit-in dhe dritaren. |
| `status` | Votat, dritarja, afati dhe nëse propozimi mund të ekzekutohet. |
| `execute` | Thërret `execute_gated_upgrade` vetëm kur kuorumi, timelock-u dhe afati e lejojnë. |

`propose` nënshkruhet nga një anëtar i multisig-ut që ka vetëm të drejtën Initiate. Një çelës i rrjedhur mund vetëm të hapë propozime. Miratimin e japin gjithmonë nënshkruesit e multisig-ut, dhe votat verifikuesit tanë, që e kapin propozimin nga eventi `ProposalCreated`.

[`packages/github-action`](packages/github-action/README.md) e ekzekuton CLI-në në repon e klientit:
- **Në çdo push:** ndërton programin me të njëjtin builder si verifikuesit, shkruan buffer-in dhe hap propozimin.
- **Me orar:** provon `execute`.

Rezultati del në përmbledhjen e workflow-it. CI-ja e provon `status` kundrejt gate-it real pas çdo cikli në validatorin lokal. Rruga e plotë me Squads dhe build-in e Action-it ende nuk është provuar në një cluster.

## Provat paralele dhe Trident

- [Tre job-e në Sprite](docs/test-results/sprite-parallel-7d57210.json). Secili ndërton kandidatin dhe e riekzekuton mbi 1,000 transaksione të veta. Matja përdor një namespace Linux pa rrjet. RAM-i i raportuar është maksimumi i një procesi fëmijë.
- [Kontrolli vetëm për lexim i Orca Mainnet](docs/test-results/orca-mainnet-preflight.json). Ky raport mat marrjen e nënshkrimeve dhe identifikon versionin e transaksionit të mostrës.
- [Fushata Trident me 1,000,000 iteracione](docs/test-results/trident-policy-1m.json). Harness-i ekzekuton ELF-in SBF të gate-it dhe krahason vendimet e `propose_config_update` me modelin e politikës për rastet e gjeneruara.
- [Cikli në Devnet pas rregullimeve](docs/test-results/devnet-two-verifiers-44924610867.json). Me Alpenglow aktiv, arkivi përdor kohën e bllokut prind. Fee payer-i i rindërtuar përfundon në gjendjen e finalizuar dhe të dy worker-at mbyllen si `executed` me votën e tyre.
- [Cikli i plotë në Devnet publik me dy verifikues në runner-a të veçantë](docs/test-results/devnet-two-verifiers-44847612055.json). `verifier-a` dhe `verifier-b` votuan me të njëjtin `diff_root`. Kuorumi përputhet me replay-in e kontrolluar dhe `execute_gated_upgrade` u finalizua. Cikli zgjati 7 minuta dhe kushtoi rreth 0.55 SOL Devnet.
- [Cikli i plotë me dy verifikues në Sprite](docs/test-results/sprite-two-verifier-rehearsal.json). Dy worker-a, secili me çelësin e vet, kapën gjendjen, ndërtuan kandidatin nga GitHub, riekzekutuan 50 transaksione dhe votuan. `diff_root` i kuorumit përputhet me replay-in e snapshot-it të kontrolluar dhe upgrade-i u ekzekutua.
- [Replay i dritares reale Devnet me vëzhgime](docs/test-results/devnet-observed-replay-44783501353.json). Builder-i i ri riprodhoi në Sprite të njëjtët bajtë si CI-ja për baseline-in dhe kandidatin.

Për ta përsëritur fushatën në Linux, ndërto më parë gate-in me komandën e MVP-së. Nga `trident-tests` ekzekuto:

```sh
CANARY_FUZZ_ITERATIONS=1000000 FUZZING_METRICS=1 TRIDENT_WITH_EXIT_CODE=1 cargo run --locked --release --bin fuzz_policy
```

Vendos `CANARY_FUZZ_SCENARIO=reclaim_buffer` per fushaten e rikuperimit te buffer-it. [Rezultatet e fushates](docs/test-results/trident-reclaim-1m.json).

Vendos `CANARY_FUZZ_SCENARIO=transfer_program_authority_guards` për kontrollet e refuzimit para CPI-së së transferimit. Prova LiteSVM ushtron rrugët e autorizuara të transferimit. [Rezultatet e fushatës në Sprite](docs/test-results/trident-transfer-guards-1m.json).

Pasi të ndërtosh edhe counter-in me `scripts/mvp.py`, vendos `CANARY_FUZZ_SCENARIO=execute_gated_upgrade_guards` për kontrollet e gjeneratës së politikës, statusit, skadimit, timelock-ut dhe ndryshimit të baseline-it. [Rezultatet e fushatës në Sprite](docs/test-results/trident-execute-guards-1m.json).

Fushata e votave Ed25519 ngeci në një degë të paimplementuar të Trident 0.12 ([diagnostika](docs/test-results/trident-attestation-compatibility.json)). Ajo kryhet në LiteSVM 0.7.1, mbi të njëjtin ELF SBF të gate-it. Çdo iteracion dërgon transaksionin real `[Ed25519SigVerify, submit_verified_attestation]`, kështu që precompile-i verifikon nënshkrimin dhe gate-i lexon offset-et nga sysvar-i i instruksioneve. Rastet zgjidhen me seed determinist: kuorumi, statusi, skadimi, dritarja, payload-i, politika e replay-it, verifikuesi i panjohur, vota e dyfishtë, rrënja e ndryshme, nënshkrimi i dëmtuar dhe offset-i jokanonik. Pas çdo refuzimi, propozimi duhet të mbetet bajt për bajt i pandryshuar. [1,000,000 iteracione në Sprite](docs/test-results/litesvm-attestation-1m.json).

```sh
target/release/canary-gate fuzz-votes target/sbpf-solana-solana/release/canary_upgrade_gate.so 1000000 1795204278
```

CI ekzekuton 20,000 iteracione pas provës së MVP-së.

### Ingress dhe statusi i worker-it

Konfigurimi i klientit pranon `repo_url` (HTTPS), `library_name`, `build_subdir`, `build_features` dhe `webhook_url`. Per t'i ruajtur me CLI perdor `--repo-url`, `--library-name`, `--build-subdir`, `--build-feature` (mund te perseritet) dhe `--webhook-url`. URL-ja e repos nuk duhet te permbaje kredenciale. Builder-i i worker-it perdor keto fusha.

- Helius dergon `POST /webhook/helius` me vleren e `HELIUS_WEBHOOK_AUTHORIZATION` ne header-in `Authorization`. Kerkesa zgjon kontrollin RPC. Vetem eventet e finalizuara nga programi gate mund te hapin job. Polling-u vazhdon si catch-up.
- `GET /status` kerkon `Authorization: Bearer <CANARY_ADMIN_TOKEN>` dhe kthen kuotat mujore te klienteve aktive dhe 100 job-et me te fundit.
- Njoftimet opsionale te operatorit perdorin `CANARY_SLACK_WEBHOOK` ose `CANARY_TELEGRAM_BOT_TOKEN` dhe `CANARY_TELEGRAM_CHAT_ID`. Cakto `CANARY_VERIFIER_PUBKEY` per njoftimin nen 0.05 SOL. Njoftimet jane best-effort dhe deshtimi i transportit nuk ndryshon vendimin e job-it.
- RPC kerkon `maxSupportedTransactionVersion: 1` per lexim. Nje transaksion v1 i lidhur me target-in e ben dritaren te paperputhshme me runner-in aktual. Worker-i regjistron refuzimin dhe liron job-in. LiteSVM vazhdon me legacy dhe v0.

Fly mban `auto_stop_machines = "off"` derisa job-et qe presin finalizimin te kene nje mekanizem zgjimi te pavarur nga webhook-u fillestar. Per cdo klient, worker-i kap gjendjen live, ndertos kandidatin nga repoja dhe voton (shih me poshte).

### Kapja live e gjendjes fillestare

Kapja live vlen për çdo klient. Te anchor-i kapen llogaritë e programit dhe metadatat e upgrade-it. `seed_accounts` liston deri në 96 llogari të tjera që transaksionet e target-it i shkruajnë, si pool-e ose mint-e ku bëhet mint ose burn. Llogaritë që dritarja vetëm i lexon, programet e tjera dhe llogaritë Token ku vetëm lëvizin tokena nuk kanë nevojë të listohen: ato lexohen pas dritares (shih [arkivin](#arkivi-i-replay-it-dhe-vota-automatike)). CLI pranon `--seed-account PUBKEY` dhe mund të përsëritet.

Pasi pranohet eventi `ProposalCreated`, worker-i lexon llogarite e programit, varesite e listuara, propozimin, buffer-in dhe ProgramData nga banka te finalizuara brenda 32 sloteve nga anchor-i. RPC-ja publike mund te finalizoje nje banke te re ndermjet leximeve. Prandaj worker-i kontrollon zinxhirin e blloqeve nga anchor-i deri te leximi i fundit dhe refuzon shkrimet e mundshme ne llogarite e kapura. Kjo deshmon se cdo lexim jep gjendjen ne fillim te slotit anchor. Rezultati `prestate_captured` tregon kapjen fillestare, jo perfundimin e replay-it. Kapja rri ne memorie. Ne disk shkruhet vetem gjate verifikimit dhe fshihet sapo job-i merr rezultat, perfundon ose skadon. Rinisja regjistron se kapja u humb.

Pas finalizimit të dritares, worker-i kontrollon kufirin e epokës dhe zbaton tavanin e klientit. Skanon zinxhirin e plotë të blloqeve deri te blloku i parë i finalizuar pas dritares, krahason listën e transaksioneve dhe refuzon shkrimet e mundshme nga transaksionet jashtë listës. Këtu hyjnë edhe tarifat e transaksioneve të dështuara. Kontrollon që çdo llogari, ALT dhe ProgramData që përdor dritarja të jetë kapur ose të lexohet pas dritares, dhe që bilancet të përputhen në përdorimin e parë. Dështimi i një kontrolli e mbyll job-in si `refused`. Statusi `traffic_preflight_passed` regjistron kalimin e këtyre kontrolleve.

### Arkivi i replay-it dhe vota automatike

Nga e njëjta skanim, worker-i ndërton arkivin që lexon `canary-gate verify`:

- Llogaritë e kapura që përdor çdo transaksion, ALT-të dhe ProgramData e programeve të tjera. Programin target e instalon vetë runner-i nga ProgramData e kapur, përfshirë mbushjen me zero.
- Llogaritë që dritarja përdor, por që nuk u kapën te anchor-i, lexohen pas dritares me `minContextSlot` pas fundit të saj:
  - Një llogari që asnjë transaksion i target-it nuk e shkruan kishte gjendjen e leximit kur e përdori dritarja. Për këtë duhet që asnjë transaksion tjetër të mos e ketë shkruar nga anchor-i deri te leximi: skanimi i dritares e kontrollon brenda saj, dhe një skanim i dytë nga fundi i dritares deri te leximi. Kështu hyjnë mint-et, oracle-t, ALT-të dhe programet e tjera bashkë me ProgramData-n e tyre. Programet kapen me bajtët që ekzekuton rrjeti, si SPL Token ose Token-2022, dhe zëvendësojnë kopjet e LiteSVM. Programet builtin i jep vetë LiteSVM. Sysvar-et e tjera dhe programet me loader v4 refuzohen.
  - Një portofol që transaksionet e target-it e shkruajnë, dhe që pas dritares është llogari System pa të dhëna, rindërtohet në përdorimin e parë me lamports nga `preBalances`. Kështu mbulohet rasti i zakonshëm në DeFi, kur portofoli i përdoruesit nënshkruan transfer-in. Një program mund vetëm ta lexojë ose t'i marrë lamports, dhe këto i kontrollojnë bilancet e çdo transaksioni. `Assign` ose `Allocate` do të linin të dhëna ose pronar tjetër, dhe kontrolli `final_accounts` i refuzon. Një portofol që dritarja e krijoi nis si llogari që nuk ekziston.
  - Një llogari Token që transaksionet e target-it e shkruajnë rindërtohet në përdorimin e parë. Çdo instruksion që e prek, edhe brenda CPI-së, duhet të jetë `Transfer`, `MintTo`, `Burn` ose varianti `Checked` i tyre, sepse këto ndryshojnë vetëm sasinë dhe, për SOL-in native, lamports. Sasia, mint-i dhe pronari merren nga `preTokenBalances`, lamports nga `preBalances`, dhe fushat e tjera nga gjendja pas dritares. Kjo gjendje hyn në `final_accounts`, ndaj runner-i kërkon që replay-i i baseline-it të arrijë saktë aty. Refuzohen që para replay-it:
    - një `Approve`;
    - një llogari pa bilanc fillestar ose me pronar të ndryshuar;
    - një debitim që nuk e nënshkruan pronari: një delegat ul edhe `delegated_amount` dhe mund ta fshijë delegatin;
    - një llogari Token-2022 me extension që ndryshon gjatë një transfer-i, si `TransferFeeAmount`. Lejohen vetëm `ImmutableOwner`, `MemoTransfer`, `CpiGuard` dhe `NonTransferableAccount`.

    Çdo fushë tjetër që ndryshon pa u regjistruar e prish përputhjen përfundimtare, dhe dritarja refuzohet.
- Feature-t që rrjeti kishte aktive te anchor-i, me slotin e aktivizimit, nga llogaritë e programit `Feature111…`. Runner-i ndërton LiteSVM me to në vend të `FeatureSet::all_enabled()`, sepse builtin-et, precompile-t dhe runtime-i i programeve varen prej tyre.
- Fee payer-i që nuk ishte në `seed_accounts` rindërtohet si llogari System pa të dhëna, me bilancin `preBalances` të përdorimit të parë. Runtime-i pranon si fee payer vetëm llogari System pa të dhëna ose llogari nonce, ndaj supozimi kontrollohet. Pas dritares, worker-i lexon gjendjen e finalizuar të payer-it dhe skanon çdo bllok nga fundi i dritares deri te ai lexim. Nëse ndonjë transaksion mund ta ketë prekur payer-in në atë hapësirë, dritarja refuzohet. Përndryshe gjendja hyn në `final_accounts` dhe runner-i kërkon që replay-i i baseline-it të përfundojë saktë aty: bilanci, pronari, të dhënat dhe flag-u executable. Një payer nonce, me 80 bajtë të dhëna, nuk e kalon këtë kontroll. Payer-i hyn edhe në kontrollin e shkrimeve jashtë listës për gjithë dritaren. Payer-i mund t'i kalohet edhe programit si llogari, p.sh. si nënshkrues i transfer-it, sepse gjendja e tij e rindërtuar ka vetëm lamports dhe kontrollohet me gjendjen përfundimtare.
- Një `Clock` për çdo slot, ashtu siç e lexojnë transaksionet e atij sloti. Me TowerBFT kjo është `blockTime` e slotit. Me Alpenglow transaksionet lexojnë `blockTime` e bllokut prind, sepse `blockTime` e një sloti është koha e footer-it pas ekzekutimit ([anza-xyz/agave#15441](https://github.com/anza-xyz/agave/issues/15441)). Worker-i e lexon këtë nga feature-i `alpenglow` (`A1pengvuM6JEcyNuTnMqepBKhwHE3N6PmUrdATGawhJS`). Në Devnet ai është aktiv që nga sloti 504,144,000. Dritaret afër aktivizimit, në epokën e tij ose në epokën pasardhëse, refuzohen. Slotet e kapërcyera mbajnë vlerën e fundit, sepse në to nuk ekzekutohet asnjë transaksion. `epoch_start_timestamp` dhe `leader_schedule_epoch` lexohen nga sysvar-i `Clock` në bankën e kapjes, që duhet të jetë në të njëjtën epokë.
- `replay_mode`, `min_replay_txs` dhe `max_cu_delta_bps` lexohen nga konfigurimi i gate-it në të njëjtën bankë. Autoriteti i buffer-it duhet të jetë PDA-ja e këtij konfigurimi.
- Për çdo transaksion: `preBalances`, `postBalances`, `computeUnitsConsumed` dhe `fee` të finalizuara. Runner-i kërkon që replay-i i baseline-it t'i riprodhojë saktë. Tarifa e pritur llogaritet nga nënshkrimet, precompile-t dhe `SetComputeUnitPrice` me limit eksplicit. LiteSVM 0.7 nuk ngarkon tarifën e prioritetit, ndaj runner-i e zbret nga fee payer-i para ekzekutimit në të dy VM-të. Bilanci i llogarisë së programit target nuk krahasohet, sepse LiteSVM e instalon me loader-in e vet.

Me `canary-gate verify` dhe këto vëzhgime, 50 transaksionet e ekzekutimit Devnet `44783501353` u riprodhuan me bilance, 557 CU dhe 5,000 lamports tarifë identike. Një CU, një tarifë ose një bilanc i ndryshuar në arkiv refuzohet.

Pastaj worker-i:

1. Shkarkon commit-in e propozimit nga `repo_url` me HTTPS anonim dhe kontrollon hash-in e tij.
2. Kopjon `build_subdir` pa `.git`, `.cargo`, `target`, `rust-toolchain*` dhe pa lidhje simbolike, bën `cargo vendor` dhe ndërton në Docker pa rrjet, pa capabilities dhe me burim read-only. `live_build.sh` përdor të njëjtin `scripts/worker_build.py`, ndaj propozuesi dhe verifikuesit ndërtojnë njësoj. `build_features` zgjedh feature-t e Cargo.
3. Ekzekuton `canary-gate verify ARCHIVE.json BASELINE.so REBUILT.so`. Kodi i daljes 3 do të thotë se vetë kandidati dështoi një kontroll: binari nuk përputhet me buffer-in ose me hash-in e propozuar, kandidati dështoi, nuk e thirri target-in, ndryshoi të dhënat e kthyera, kaloi kufirin CU ose ndryshoi gjendjen në `StrictEquality`. Kjo e mbyll job-in si `rejected` dhe dërgon `CandidateRejected`. Çdo gabim tjetër, si baseline që dështon, gjendje që mungon, vëzhgim që nuk përputhet ose tarifë që nuk mbështetet, e mbyll job-in si `refused` me arsyen `replay evidence incomplete` dhe dërgon `ReplayFailed`. Kandidati nuk gjykohet.
4. Nënshkruan payload-in 186 bajtësh me çelësin e nyjës dhe dërgon `[Ed25519SigVerify, submit_verified_attestation]`. Pas finalizimit regjistron `voted` në SQLite dhe dërgon `VoteSubmitted`. `UpgradeExecuted` e kalon më pas në `executed`.

Leximi i eventeve kërkon `minContextSlot` të barabartë me slotin e fundit të finalizuar. Një endpoint me disa nyje mund ta dërgojë kërkesën te një nyje që ende nuk e ka indeksuar atë slot. Atëherë worker-i pret dhe e lexon sërish të njëjtën hapësirë, në vend që të kalojë përtej eventeve. Në Devnet, pa këtë kontroll, `verifier-a` humbi `UpgradeExecuted`. Nëse upgrade-i ekzekutohet ndërsa vota e kësaj nyjeje po finalizohet, job-i mbetet i hapur derisa të regjistrohet vota, pastaj kalon në `executed`.

Ndërtimi i arkivit, build-i, replay-i dhe pritja e finalizimit të votës ekzekutohen në një thread të veçantë, një job në një kohë. Loop-i i eventeve vazhdon të lexojë `ProposalCreated` dhe kap propozimet e reja brenda kufirit prej 32 slotesh, edhe kur një build zgjat minuta.

Gabimet e rrjetit dhe RPC-së provohen sërish në ciklin tjetër. Dështimi i build-it provohet tri herë. Gjatë job-it, `archive.json`, `baseline.so` dhe `build/` shkruhen në `/data/jobs/<proposal>/`, sepse `canary-gate` i lexon nga disku. Kur job-i merr një rezultat, ose kur upgrade-i ekzekutohet, këto fshihen dhe mbetet vetëm `evidence.json`, që përmban hash-e dhe numërime. Pas një rinisjeje, worker-i i fshin për të gjitha job-et. `CANARY_KEEP_JOB_FILES=1` i ruan, dhe përdoret vetëm në provat CI për artefaktet.

| Variabla | Roli |
| --- | --- |
| `CANARY_VERIFIER_KEYPAIR` ose `CANARY_VERIFIER_KEYPAIR_FILE` | Keypair-i 64 bajtësh i verifikuesit. Paguan edhe tarifën e votës. Pa të, ose kur çelësi nuk është në politikë, job-i mbyllet si `verified` pa votë. |
| `CANARY_GATE_BIN` | Rruga e `canary-gate`, i ndërtuar me ID-në e gate-it të publikuar. |
| `CANARY_SBF_TOOLCHAIN` | Toolchain-i rustup i lidhur me platform-tools v1.58. Pa këtë dhe `CANARY_GATE_BIN`, worker-i ndalet te `traffic_preflight_passed`. |
| `CANARY_DOCKER_RESOURCE_LIMITS=0` | Heq `--cpus` dhe `--memory` në hostet me cgroup në modalitet threaded, si Sprites. Bajtët e binarit nuk ndryshojnë. |
| `CANARY_DOCKER_ROOT=1` | Për hostet ku Docker nis vetëm uid 0. Kontejneri mbetet pa rrjet dhe pa capabilities dhe ua kthen skedarët e daljes të lexueshëm. |
| `CANARY_CLUSTER` | `devnet` (vlera e paracaktuar) ose `mainnet-beta`. Genesis-i i `RPC_URL` duhet të përputhet. |
| `CANARY_BUILDER_HOST` (opsional) | Host-i i veçantë i build-it, që arrihet me SSH. Pa të, worker-i ndërton vetë në Docker. |
| `CANARY_EPHEMERAL_CARGO_HOME=1` | `cargo vendor` përdor një Cargo home brenda direktorisë së përkohshme të build-it, që fshihet bashkë me të. Kështu crate-t e klientëve nuk grumbullohen në volum. Imazhi i builder-it fikson `ubuntu:24.04` me digest dhe e merr emrin nga Dockerfile-i, ndaj një ndryshim i Dockerfile-it e rindërton. Paketat `apt` nuk janë të fiksuara, por ato prekin vetëm build script-et, jo kompiluesin SBF. |

Për build-in duhen Docker dhe platform-tools. Pa `CANARY_GATE_BIN` dhe toolchain-in, worker-i punon vetëm si listener dhe ndalet te `traffic_preflight_passed`. Për votim përdor imazhin `deploy/fly/verifier`, të përshkruar më sipër.

### Çfarë mungon për Mainnet

`CANARY_CLUSTER=mainnet-beta` e lejon worker-in të lexojë Mainnet. Arkivi tani përfshin programet e varura me bajtët e rrjetit, llogaritë Token ku vetëm lëvizin tokena dhe feature-t aktive (shih [arkivin](#arkivi-i-replay-it-dhe-vota-automatike)). Mbeten:

- **Runtime-i i LiteSVM.** LiteSVM 0.7.1 përdor runtime-in agave 2.3. Një feature që rrjeti e aktivizoi në një version më të ri nuk ka efekt në replay. Kur kjo ndryshon CU-të ose rezultatin, baseline-i nuk përputhet me zinxhirin dhe dritarja refuzohet. Zgjidhja është përditësimi i LiteSVM drejt versionit që ekzekuton rrjeti.
- **Llogaritë Token me ndryshime të tjera.** Refuzohen llogaritë e krijuara ose të mbyllura në dritare, `Approve`, `SetAuthority`, `Freeze`, debitimet nga delegatët, extension-et e Token-2022 që ndryshojnë dhe instruksionet e tyre. Edhe mint-et ku bëhet mint ose burn duhet të jenë në `seed_accounts`.
- **Sysvar-et.** Përveç `Clock` dhe `Instructions`, një transaksion që lexon një sysvar, si `Rent` ose `SlotHashes`, refuzohet.
- **Prova live.** Rruga e re është provuar me teste njësi dhe me replay-in e demos. Ende nuk është provuar me trafik real DeFi në Devnet ose Mainnet.
- **Gate-i dhe build-i.** Gate-i duhet audituar dhe publikuar në Mainnet. Build-i rekomandohet të kalojë në një makinë pa çelës votimi.
