mod args;

use std::collections::BTreeMap;
use std::io::{self, Read, Write};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{SystemTime, UNIX_EPOCH};

use args::{filter_envvars, CliArgs, EnvArg};
use clap::Parser;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
enum Dest {
    Out,
    Err,
}

#[derive(Serialize, Deserialize)]
struct Output {
    dest: Dest,
    data: Vec<u8>,
}

#[derive(Serialize, Deserialize)]
struct RunResult {
    started_at: f64, // unix seconds, sub-second precision
    return_code: i32,
    outputs: Vec<Output>,
}

/// The subset of config that determines the cache key. Env values are pre-hashed by the
/// caller so secrets never touch disk. Serialized deterministically (BTreeMap) then
/// sha256'd to form the cache filename.
#[derive(Serialize)]
struct CacheKey<'a> {
    command: &'a [String],
    shell: bool,
    shlex: bool,
    input: &'a Option<String>,
    env: &'a BTreeMap<String, String>, // already hashed
}

fn log(level: &str, quiet: bool, verbose: bool, msg: &str) {
    let show = match level {
        "DEBUG" => verbose,
        "INFO" => !quiet,
        _ => true, // WARN/ERROR always
    };
    if show {
        eprintln!("[runcached:{level}] {msg}");
    }
}

fn strip_ansi(data: &[u8]) -> Vec<u8> {
    // https://stackoverflow.com/a/14693789
    use regex::bytes::Regex;
    use std::sync::OnceLock;
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| {
        Regex::new(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").unwrap()
    });
    re.replace_all(data, &b""[..]).into_owned()
}

fn write_output(o: &Output, strip: bool) {
    let bytes = if strip { strip_ansi(&o.data) } else { o.data.clone() };
    match o.dest {
        Dest::Out => {
            let _ = io::stdout().write_all(&bytes);
            let _ = io::stdout().flush();
        }
        Dest::Err => {
            let _ = io::stderr().write_all(&bytes);
            let _ = io::stderr().flush();
        }
    }
}

fn now_secs() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

/// Run the command, capturing stdout/stderr and streaming them live to our own
/// stdout/stderr. Interleaving order across the two streams is approximate (inherent to
/// captured pipes), matching the Python tool's line-oriented merge.
fn run_uncached(
    command: &[String],
    env: &BTreeMap<String, String>,
    input: &Option<String>,
    shell: bool,
    shlex: bool,
    strip: bool,
) -> io::Result<RunResult> {
    let started_at = now_secs();

    let mut cmd = if shell {
        let joined = if shlex {
            shlex::try_join(command.iter().map(|s| s.as_str()))
                .unwrap_or_else(|_| command.join(" "))
        } else {
            command.join(" ")
        };
        let sh = env.get("SHELL").cloned().unwrap_or_else(|| "sh".to_string());
        let mut c = Command::new(sh);
        c.arg("-c").arg(joined);
        c
    } else {
        let mut c = Command::new(&command[0]);
        c.args(&command[1..]);
        c
    };

    cmd.env_clear().envs(env);
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    cmd.stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() });

    let mut child = cmd.spawn()?;

    if let (Some(inp), Some(mut sink)) = (input.as_ref(), child.stdin.take()) {
        let inp = inp.clone();
        thread::spawn(move || {
            let _ = sink.write_all(inp.as_bytes());
        });
    }

    let (tx, rx) = mpsc::channel::<Output>();
    let mut readers = Vec::new();
    let pipes: [(Dest, Option<Box<dyn Read + Send>>); 2] = [
        (Dest::Out, child.stdout.take().map(|p| Box::new(p) as Box<dyn Read + Send>)),
        (Dest::Err, child.stderr.take().map(|p| Box::new(p) as Box<dyn Read + Send>)),
    ];
    for (dest, pipe) in pipes {
        if let Some(mut pipe) = pipe {
            let tx = tx.clone();
            readers.push(thread::spawn(move || {
                let mut buf = [0u8; 8192];
                loop {
                    match pipe.read(&mut buf) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => {
                            if tx.send(Output { dest, data: buf[..n].to_vec() }).is_err() {
                                break;
                            }
                        }
                    }
                }
            }));
        }
    }
    drop(tx);

    let mut outputs = Vec::new();
    for o in rx {
        write_output(&o, strip);
        outputs.push(o);
    }
    for r in readers {
        let _ = r.join();
    }

    let status = child.wait()?;
    let return_code = status.code().unwrap_or(-1);

    Ok(RunResult { started_at, return_code, outputs })
}

fn cache_key(
    command: &[String],
    env_hashed: &BTreeMap<String, String>,
    input: &Option<String>,
    shell: bool,
    shlex: bool,
) -> String {
    let key = CacheKey { command, shell, shlex, input, env: env_hashed };
    let json = serde_json::to_vec(&key).unwrap();
    let mut h = Sha256::new();
    h.update(&json);
    format!("{:x}", h.finalize())
}

fn hash_env(env: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    env.iter()
        .map(|(k, v)| {
            let mut h = Sha256::new();
            h.update(v.as_bytes());
            (k.clone(), format!("{:x}", h.finalize()))
        })
        .collect()
}

fn main() {
    let args = CliArgs::parse();
    let quiet = args.quiet;
    let verbose = args.verbose;

    let command = args.command_words();
    if command.is_empty() {
        // Mirror argparse: print help and exit non-zero.
        use clap::CommandFactory;
        let _ = CliArgs::command().print_help();
        std::process::exit(1);
    }

    let os_env: BTreeMap<String, String> = std::env::vars().collect();

    let exclude_pats: Vec<String> = args.excludes().into_iter().map(|e| e.name).collect();
    let includes: Vec<EnvArg> = args.includes();
    let passthru: Vec<EnvArg> = args.passthru();

    let mut envs_for_cache = filter_envvars(&os_env, &includes, &exclude_pats);
    let envs_for_passthru = filter_envvars(&os_env, &passthru, &exclude_pats);

    if args.shell() {
        envs_for_cache.insert(
            "SHELL".to_string(),
            os_env.get("SHELL").cloned().unwrap_or_else(|| "sh".to_string()),
        );
    }

    let input = if args.stdin() {
        let mut s = String::new();
        let _ = io::stdin().read_to_string(&mut s);
        Some(s)
    } else {
        None
    };

    // Child env = cache env + passthru env (passthru wins on conflict, per Python order).
    let mut child_env = envs_for_cache.clone();
    for (k, v) in &envs_for_passthru {
        child_env.insert(k.clone(), v.clone());
    }

    let strip = args.strip_colors();
    let shell = args.shell();
    let shlex = args.shlex();

    let cache_dir = directories::ProjectDirs::from("", "", "runcached")
        .map(|d| d.cache_dir().to_path_buf())
        .unwrap_or_else(|| std::env::temp_dir().join("runcached"));
    let _ = std::fs::create_dir_all(&cache_dir);

    let key = cache_key(&command, &hash_env(&envs_for_cache), &input, shell, shlex);
    let cache_file = cache_dir.join(format!("{key}.json"));

    let min_started_at = now_secs() - args.ttl.as_secs_f64();

    // Cache hit?
    if let Ok(bytes) = std::fs::read(&cache_file) {
        if let Ok(result) = serde_json::from_slice::<RunResult>(&bytes) {
            if result.started_at >= min_started_at {
                log("INFO", quiet, verbose,
                    &format!("Using cached result from {}.", result.started_at));
                for o in &result.outputs {
                    write_output(o, strip);
                }
                std::process::exit(result.return_code);
            }
        }
    }

    // Miss: run it.
    let result = match run_uncached(&command, &child_env, &input, shell, shlex, strip) {
        Ok(r) => r,
        Err(e) => {
            log("ERROR", quiet, verbose, &format!("Failed to run command: {e}"));
            std::process::exit(127);
        }
    };

    if result.return_code == 0 || args.keep_failures {
        if let Ok(bytes) = serde_json::to_vec(&result) {
            let _ = std::fs::write(&cache_file, bytes);
        }
    } else {
        log("WARN", quiet, verbose, &format!(
            "Command returned {} and --keep-failures not specified; refusing to cache.",
            result.return_code
        ));
    }

    std::process::exit(result.return_code);
}
