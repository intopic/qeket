mod demo;
mod replay;

use anyhow::{bail, Context, Result};
use std::{env, fs, path::Path};

fn main() -> Result<()> {
    let args: Vec<_> = env::args().skip(1).collect();
    match args.as_slice() {
        [command, gate, base, candidate, rebuilt, commit, out] if command == "demo" => {
            demo::run(
                &fs::read(gate)?,
                &fs::read(base)?,
                &fs::read(candidate)?,
                &fs::read(rebuilt)?,
                commit,
                Path::new(out),
            )
        }
        [command, archive, base, rebuilt] if command == "verify" => {
            let archive: replay::Archive = serde_json::from_reader(fs::File::open(archive)?).context("invalid local archive")?;
            let evidence = replay::verify(&archive, &fs::read(base)?, &fs::read(rebuilt)?)?;
            println!("{}", serde_json::to_string_pretty(&evidence)?);
            Ok(())
        }
        _ => bail!("Usage: canary-gate demo GATE.so OLD.so NEW.so REBUILT.so GIT_SHA OUTPUT_DIR\n       canary-gate verify ARCHIVE.json OLD.so REBUILT.so"),
    }
}
