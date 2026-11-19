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
