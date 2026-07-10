use std::collections::BTreeMap;
use std::time::Duration;

use clap::Parser;

/// Runs the given command with caching of stdout and stderr.
#[derive(Parser, Debug)]
#[command(name = "runcached", about, disable_help_flag = false)]
pub struct CliArgs {
    /// Max length of time for which to cache command results.
    /// Format: humantime (e.g. "1d", "2h30m") or bare seconds. [default: 1d]
    #[arg(long, short = 't', value_name = "DURATION", default_value = "1d",
          value_parser = parse_ttl)]
    pub ttl: Duration,

    /// Cache run results that exit non-zero. Not cached by default.
    #[arg(long, short = 'F')]
    pub keep_failures: bool,

    /// Include stdin when computing cache key. Defaults to true if stdin is not a TTY.
    /// If included, stdin is read until EOF before executing.
    #[arg(long = "include-stdin", short = 'i', overrides_with = "no_stdin")]
    include_stdin: bool,
    /// Exclude stdin when computing cache key. Overrides -i.
    #[arg(long = "exclude-stdin", short = 'I')]
    no_stdin: bool,

    /// Include named environment variable(s) when running command and computing cache
    /// key. Comma/space separated, shell-quoting respected. VAR forwards existing value,
    /// VAR=value assigns. Wildcards allowed when forwarding. Aggregates across options.
    #[arg(long = "include-env", short = 'e', value_name = "VAR[,...]",
          value_parser = parse_env_args)]
    include_env: Vec<Vec<EnvArg>>,

    /// Pass named env var(s) through to command without caching them. Same format as -e.
    /// [default: HOME,PATH,TMPDIR]
    #[arg(long = "passthru-env", short = 'p', value_name = "VAR[,...]",
          value_parser = parse_env_args)]
    passthru_env: Vec<Vec<EnvArg>>,

    /// Do not pass named env var(s) through, nor include in cache key. Assignments
    /// disallowed. Overrides -e and -p.
    #[arg(long = "exclude-env", short = 'E', value_name = "VAR[,...]",
          value_parser = parse_env_args)]
    exclude_env: Vec<Vec<EnvArg>>,

    /// Pass command to $SHELL for execution.
    #[arg(long, short = 's', overrides_with = "no_shell")]
    shell: bool,
    /// Do not pass command to $SHELL. Overrides -s.
    #[arg(long = "no-shell", short = 'S')]
    no_shell: bool,

    /// Re-quote command line args before passing to $SHELL. Only used if shell is true.
    #[arg(long, short = 'l', overrides_with = "no_shlex")]
    shlex: bool,
    /// Do not re-quote command line args before passing to $SHELL. Overrides -l.
    #[arg(long = "no-shlex", short = 'L')]
    no_shlex: bool,

    /// Strip ANSI escape sequences from cached output. Defaults to true if stdout is not
    /// a TTY.
    #[arg(long = "strip-colors", short = 'C', overrides_with = "no_strip_colors")]
    strip_colors: bool,
    /// Do not strip ANSI escape sequences. Overrides -C.
    #[arg(long = "no-strip-colors", short = 'c')]
    no_strip_colors: bool,

    /// Set log level to warnings only.
    #[arg(long, short = 'q')]
    pub quiet: bool,
    /// Set log level to debug.
    #[arg(long, short = 'v')]
    pub verbose: bool,

    /// Command and arguments to run.
    #[arg(
        value_name = "COMMAND",
        trailing_var_arg = true,
        allow_hyphen_values = true
    )]
    pub command: Vec<String>,
}

impl CliArgs {
    /// Resolved tri-state flags, applying the stdin/strip-colors TTY defaults.
    pub fn stdin(&self) -> bool {
        // -I wins; else -i; else default (stdin not a tty)
        if self.no_stdin {
            false
        } else if self.include_stdin {
            true
        } else {
            !atty(libc_stdin())
        }
    }
    pub fn shell(&self) -> bool {
        self.shell && !self.no_shell
    }
    pub fn shlex(&self) -> bool {
        self.shlex && !self.no_shlex
    }
    pub fn strip_colors(&self) -> bool {
        if self.no_strip_colors {
            false
        } else if self.strip_colors {
            true
        } else {
            !atty(libc_stdout())
        }
    }

    pub fn includes(&self) -> Vec<EnvArg> {
        self.include_env.iter().flatten().cloned().collect()
    }
    pub fn passthru(&self) -> Vec<EnvArg> {
        if self.passthru_env.is_empty() {
            parse_env_args("HOME,PATH,TMPDIR").unwrap()
        } else {
            self.passthru_env.iter().flatten().cloned().collect()
        }
    }
    pub fn excludes(&self) -> Vec<EnvArg> {
        self.exclude_env.iter().flatten().cloned().collect()
    }

    /// Strip a leading `--` separator from the command, mirroring the Python behavior.
    pub fn command_words(&self) -> Vec<String> {
        match self.command.split_first() {
            Some((first, rest)) if first == "--" => rest.to_vec(),
            _ => self.command.clone(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct EnvArg {
    pub name: String,
    pub value: Option<String>,
}

/// Parse a single `-e`/`-p`/`-E` argument value into one or more EnvArgs.
/// Comma or whitespace separated, with shell-style quoting.
fn parse_env_args(arg: &str) -> Result<Vec<EnvArg>, String> {
    // Split on commas, then shlex each piece to unnest quotes (matches Python).
    let mut out = Vec::new();
    for piece in arg.split(',') {
        let piece = piece.trim();
        if piece.is_empty() {
            continue;
        }
        let words = shlex::split(piece).ok_or_else(|| format!("bad quoting: {arg}"))?;
        for w in words {
            out.push(parse_one_env_arg(&w)?);
        }
    }
    Ok(out)
}

fn parse_one_env_arg(s: &str) -> Result<EnvArg, String> {
    if let Some((name, value)) = s.split_once('=') {
        if name.is_empty() || !name.chars().all(|c| c.is_alphanumeric() || c == '_') {
            return Err(format!(
                "cannot assign value to env var with wildcards: {s}"
            ));
        }
        Ok(EnvArg {
            name: name.to_string(),
            value: Some(value.to_string()),
        })
    } else {
        Ok(EnvArg {
            name: s.to_string(),
            value: None,
        })
    }
}

fn parse_ttl(s: &str) -> Result<Duration, String> {
    if let Ok(d) = humantime::parse_duration(s) {
        return Ok(d);
    }
    if let Ok(secs) = s.parse::<u64>() {
        return Ok(Duration::from_secs(secs));
    }
    Err(format!("could not parse duration: {s}"))
}

/// Filter env vars per include patterns/assignments and exclude patterns.
/// Assignments (VAR=value) are always included with their assigned value.
/// Bare names are treated as fnmatch patterns (a literal name matches itself).
pub fn filter_envvars(
    env: &BTreeMap<String, String>,
    includes: &[EnvArg],
    exclude_pats: &[String],
) -> BTreeMap<String, String> {
    let mut merged = env.clone();
    let mut assigned = std::collections::BTreeSet::new();
    let mut include_pats = Vec::new();
    for e in includes {
        match &e.value {
            Some(v) => {
                merged.insert(e.name.clone(), v.clone());
                assigned.insert(e.name.clone());
            }
            None => include_pats.push(e.name.clone()),
        }
    }

    merged
        .into_iter()
        .filter(|(k, _)| {
            let included = assigned.contains(k) || include_pats.iter().any(|p| fnmatch(k, p));
            let excluded = exclude_pats.iter().any(|p| fnmatch(k, p));
            included && !excluded
        })
        .collect()
}

fn fnmatch(name: &str, pat: &str) -> bool {
    if name == pat {
        return true;
    }
    glob::Pattern::new(pat)
        .map(|p| p.matches(name))
        .unwrap_or(false)
}

// --- tiny libc-free tty check ---
fn libc_stdin() -> i32 {
    0
}
fn libc_stdout() -> i32 {
    1
}
fn atty(fd: i32) -> bool {
    // SAFETY: isatty is always safe to call with any int.
    unsafe { isatty(fd) == 1 }
}
extern "C" {
    fn isatty(fd: i32) -> i32;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn forwards_named_var() {
        let e = env(&[("FOO", "1"), ("BAR", "2")]);
        let inc = vec![EnvArg {
            name: "FOO".into(),
            value: None,
        }];
        let out = filter_envvars(&e, &inc, &[]);
        assert_eq!(out.get("FOO"), Some(&"1".to_string()));
        assert!(!out.contains_key("BAR"));
    }

    #[test]
    fn assignment_is_included() {
        // Python drops this (bug); intended behavior keeps it.
        let e = env(&[("BAR", "2")]);
        let inc = vec![EnvArg {
            name: "FOO".into(),
            value: Some("bar".into()),
        }];
        let out = filter_envvars(&e, &inc, &[]);
        assert_eq!(out.get("FOO"), Some(&"bar".to_string()));
    }

    #[test]
    fn wildcard_and_exclude() {
        let e = env(&[("AWS_KEY", "k"), ("AWS_SECRET", "s"), ("HOME", "h")]);
        let inc = vec![EnvArg {
            name: "AWS_*".into(),
            value: None,
        }];
        let out = filter_envvars(&e, &inc, &["AWS_SECRET".to_string()]);
        assert_eq!(out.get("AWS_KEY"), Some(&"k".to_string()));
        assert!(!out.contains_key("AWS_SECRET"));
        assert!(!out.contains_key("HOME"));
    }

    #[test]
    fn parse_env_comma_and_quotes() {
        let got = parse_env_args("FOO,BAR=baz").unwrap();
        assert_eq!(
            got[0],
            EnvArg {
                name: "FOO".into(),
                value: None
            }
        );
        assert_eq!(
            got[1],
            EnvArg {
                name: "BAR".into(),
                value: Some("baz".into())
            }
        );
    }

    #[test]
    fn ttl_parses() {
        assert_eq!(parse_ttl("1d").unwrap(), Duration::from_secs(86400));
        assert_eq!(parse_ttl("30").unwrap(), Duration::from_secs(30));
    }
}
