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
3. Një VM ekzekuton 252 transaksione lokale të nënshkruara mbi programin bazë. Arkivi ruan pozicionin e secilit në bllok. Secili nga dy verifikuesit krahason ndërtimin përkatës me buffer-in dhe riekzekuton të njëjtat transaksione në dy VM me gjendje fillestare të njëjtë. Runner-i refuzon një transaksion që dështon, kthim ose gjendje të ndryshme, tejkalim të kufirit CU dhe mospërputhje të listës së regjistruar të transaksioneve.
4. Dy nënshkrime Ed25519 mbi të njëjtën rrënjë plotësojnë kuorumin 2/3. Prova kontrollon refuzimin e votës së dyfishtë, ekzekutimin para kuorumit dhe ekzekutimin gjatë timelock-ut. Instruksioni kompakt lexon direkt mesazhin prej 186 bajtësh nga precompile-i i mëparshëm. Forma origjinale që e përsërit payload-in mbetet e mbështetur.
5. Pas timelock-ut, kontrata thërret loader-in me PDA-në. Prova kontrollon bajtët e vendosur në ProgramData, ekzekuton versionin e ri dhe lexon vlerën që ai shkroi. Në një kopje tjetër të VM-së provohen rotacioni i politikës, anulimi, mbyllja e propozimit dhe rikuperimi nga autoriteti emergjent.

## Matjet në LiteSVM

| Veprimi | Madhësia e transaksionit | Compute units |
| --- | ---: | ---: |
| Vota e parë kompakte | 610 B | 7 865 |
| Vota që plotëson kuorumin | 610 B | 7 893 |
| Ekzekutimi i përditësimit | 442 B | 16 458 |

Këto matje i përkasin kontratës, programit shembull dhe konfigurimit lokal të testuar. Komanda dështon nëse një nga votat ose ekzekutimi arrin 650 bajtë ose 25 000 CU.

## Kodi dhe provat

- `programs/canary_upgrade_gate` përmban kontratën. Ndryshimet e politikës përdorin një PDA të përkohshme dhe timelock-un aktual. Rotacioni ndryshon gjeneratën e nonce-it dhe i zhvlerëson propozimet e vjetra. Ato mund të anulohen dhe mbyllen.
- `crates/canary-types` përcakton mesazhin Borsh. `crates/svm-canary-runner` përmban verifikuesin, CLI-në dhe skenarin e plotë LiteSVM.
- `examples/counter` është programi i provës. V1 dhe V2 shkruajnë të njëjtën gjendje dhe emetojnë mesazhe të ndryshme në log, që ekzekutimi i V2 të dallohet pas përditësimit.
- `cargo test --workspace --locked` ekzekuton testet Rust. `cargo fmt --all --check` kontrollon formatimin. Në Windows testet e workspace-it përdorin të njëjtat variabla OpenSSL si CLI-ja.
- `.github/workflows/local-mvp.yml` përmban hapat e CI dhe të provës RPC në Solana Devnet.

## Kufijtë e provës lokale

Arkivi përmban transaksione legacy me thirrje direkte te programi shembull dhe gjendje sintetike. Politika lokale kërkon barazi të plotë të llogarive të shkruara dhe të vlerave të kthyera. Rrënja përmbledh gjendjen pas çdo transaksioni në rendin e slotit, indeksit dhe çelësit të llogarisë. Kontrolli i manifestit zbulon ndryshime ndaj listës së ruajtur nga regjistruesi lokal. Besimi tek origjina e arkivit mbetet tek ky regjistrues.

Dy verifikuesit janë simulime në të njëjtin kompjuter. Prova përdor çelësa të veçantë lokalë për qeverisjen dhe emergjencën. Për prodhim mbeten operatorë të pavarur, integrimi Squads/Realms dhe KMS/HSM, izolimi hermetik i build-it, marrja e të dhënave të finalizuara nga Yellowstone, replay me gjendjen historike dhe programet e varura, fuzzing me Trident, prova afatgjatë në Devnet dhe auditimi i jashtëm.
