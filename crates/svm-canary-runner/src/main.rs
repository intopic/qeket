mod demo;
mod replay;

use anyhow::{bail, Context, Result};
use std::{env, fs, path::Path};

const CANDIDATE_FAULT_EXIT: i32 = 3;

fn main() -> Result<()> {
    let args: Vec<_> = env::args().skip(1).collect();
    match args.as_slice() {
        [command, gate, base, candidate, rebuilt, commit, out, regression @ ..]
            if command == "demo" && regression.len() <= 1 =>
        {
            let regression = regression.first().map(fs::read).transpose()?;
            demo::run(
                &fs::read(gate)?,
                &fs::read(base)?,
                &fs::read(candidate)?,
                &fs::read(rebuilt)?,
                commit,
                Path::new(out),
                regression.as_deref(),
            )
        }
        [command, archive, base, rebuilt] if command == "verify" => {
            let archive: replay::Archive = serde_json::from_reader(fs::File::open(archive)?).context("invalid local archive")?;
            match replay::verify(&archive, &fs::read(base)?, &fs::read(rebuilt)?) {
                Ok(evidence) => {
                    println!("{}", serde_json::to_string_pretty(&evidence)?);
                    Ok(())
                }
                // Workers treat this exit code as a rejected candidate and any
                // other failure as incomplete evidence.
                Err(error) if replay::is_candidate_fault(&error) => {
                    eprintln!("Error: {error:#}");
                    std::process::exit(CANDIDATE_FAULT_EXIT)
                }
                Err(error) => Err(error),
            }
        }
        [command, gate, iterations, seed] if command == "fuzz-votes" => {
            demo::fuzz_votes(&fs::read(gate)?, iterations.parse()?, seed.parse()?)
        }
        _ => bail!("Usage: canary-gate demo GATE.so OLD.so NEW.so REBUILT.so GIT_SHA OUTPUT_DIR [REGRESSION.so]\n       canary-gate verify ARCHIVE.json OLD.so REBUILT.so\n       canary-gate fuzz-votes GATE.so ITERATIONS SEED"),
    }
}
