# Canary Upgrade Gate

MVP lokal për përditësime të kontrolluara të programeve Solana. Një komandë ndërton dy versione të një programi Rust, rindërton kandidatin nga i njëjti commit dhe provon përditësimin me loader-in real të Solana brenda LiteSVM. Të gjithë çelësat e votimit krijohen në memorie për atë ekzekutim.

## Ekzekutimi

Në GitHub hap **Actions → Canary MVP**. Çdo push ose pull request nis testet në Ubuntu. Mund t'i nisësh edhe nga **Run workflow**. Job-i `verify` ndërton me platform-tools v1.58 të kontrolluar me SHA-256, ekzekuton testet Rust dhe provën në LiteSVM, pastaj ruan binarët dhe arkivin si artifact të commit-it.

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
| `FLY_API_TOKEN` (opsional) | Token për deploy të worker-it. Ky hap kërkon edhe secret `CANARY_CUSTOMERS`. Për një aplikacion ekzistues vendos edhe repository variable `FLY_APP`. Për krijim automatik token-i duhet të ketë leje në organizatën `personal`. |

Nis **Actions → Live Devnet counter → Run workflow** në `main`. Një push provon ndërtimin offline. Vetëm nisja manuale aktivizon deploy dhe transaksionet. Dy votuesit Devnet përdorin çelësa të ndryshëm të derivuar në të njëjtin runner. Çdo nisje e re krijon target të ri dhe konsumon rent Devnet. Nëse një nisje dështon pasi ka inicializuar gate-in, artifact-i përmban hapat e finalizuar deri aty. Ruaj payer-in dhe run ID për rikuperimin e autoriteteve.

Artifact-i `devnet-live-<run_id>` përmban `transactions.md` dhe arkivin e replay-it. Provën mund ta nisësh me 50–100 transaksione të counter-it, secila me ComputeBudget. Snapshot-i i shembullit merret nga RPC para propozimit për counter-in dhe fee payer-in e izoluar. Workflow kërkon pikërisht transaksionet e pritura në dritaren e finalizuar dhe rikonfirmon bytecode-in e instaluar. Counter-i nuk lexon sysvar Clock. Për programe arbitrare, komanda `python scripts/live_capture.py capture SNAPSHOT.json ARCHIVE.json --max-replay-txs 500` kërkon gjendjen historike të fillimit të dritares, përfshirë ALT, ProgramData dhe sysvars përkatëse. `minContextSlot` nuk rikthen gjendjen historike. CLI ekzistuese `canary-gate verify` riekzekuton arkivin. Worker-i Fly ndjek eventet e finalizuara dhe i shkruan në log. Votimi dhe ndërtimi i kandidatit për provën Devnet kryhen në Actions.


### Whitelist dhe kuota për listener-in Fly.io

Vendos `CANARY_CUSTOMERS` si repository secret para se të aktivizosh deploy-in në Fly. Formati është një objekt JSON me ID të qëndrueshme të klientëve.

```json
{
  "protocol-a": {
    "targets": ["<program-id>"],
    "active_until": "2027-01-01T00:00:00Z",
    "monthly_proposals": 25
  }
}
```

Zëvendëso `<program-id>` me adresën publike të programit të klientit. Afati është ekskluziv. Kuota mat propozimet unike të pranuara për klientin, brenda muajit kalendarik UTC, dhe ndahet ndërmjet programeve të tij. Një propozim i pranuar ruhet vetëm një herë. Listener-i ruan numërimin dhe pozicionin e leximit në SQLite në volumin e instancës Fly. Konfigurimi aktual presupozon një instancë të vetme për këtë ledger. Politikat e klientëve mirëmbahen në secret dhe hyjnë në fuqi kur publikohet konfigurimi i ri. Kjo fazë filtron eventet që publikohen në log.


Planet `starter`, `pro` dhe `enterprise` caktojnë përkatësisht 5, 20 dhe pa kufi propozime në muaj, me tavane prej 500, 2,000 dhe 5,000 transaksionesh për replay. Mund të caktohen `active`, `cooldown_minutes`, `max_upgrades_per_month` dhe `max_replay_txs`. Përditëso `CANARY_CUSTOMERS` ose menaxho skedarin `/data/customers.json` me `python scripts/customer_admin.py --file customers.json set CLIENT --plan starter --target PROGRAM`. Për një klient ekzistues përdor `suspend CLIENT` ose `plan CLIENT --plan pro`. Skedari në volum ka përparësi ndaj secret-it. Një job i pranuar bllokon pranimin e job-eve të tjera të klientit deri në mbyllje. Në fazën aktuale operatori mund ta mbyllë me `python fly_listener.py --finish PROPOSAL`. Tavani i replay-it ruhet në payload-in e job-it. SQLite zbaton kuotën për një instancë. Për kuotë të përbashkët mes nyjeve, kërkohet ruajtje e përbashkët e vendimeve të pranimit.

### Adapteri Squads v4

`packages/squads-adapter` ndërton instruksionet e gate-it dhe i mbështjell me `vaultTransactionCreate` nga SDK Squads. Kalo adresën e gate-it të publikuar, adresën e multisig-ut, indeksin e Vault-it dhe të dhënat aktuale të propozimit. Vault-i duhet të jetë autoriteti për veprimet e qeverisjes dhe të ketë SOL për llogaritë e përdorura gjatë inicializimit ose propozimit. Ndiq rrjedhën e zakonshme Squads për krijimin e propozimit, miratimet dhe ekzekutimin. Për transferimin e autoritetit, rruga e qeverisjes kërkon `proposeProgramAuthorityTransfer` dhe timelock. Në ekzekutim duhet të sigurohet edhe nënshkrimi i marrësit të ri, sipas mënyrës si ai autoritet nënshkruan. Paketa krijon instruksionet dhe payload-in e Vault-it. Integrimi me një Vault të publikuar kërkon ende test on-chain.
