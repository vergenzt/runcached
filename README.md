# runcached

Utility to run shell commands with output caching.

<!--[[[cog
  import os, subprocess as sp
  cog.outl('```')
  cog.out(sp.run(['runcached', '-h'], env={**os.environ, 'COLUMNS': '100'}, stdout=sp.PIPE, text=True).stdout)
  cog.outl('```')
]]]-->
```
usage: runcached [-h] [--ttl DURATION] [--keep-failures] [--include-stdin] [--exclude-stdin]
                 [--include-env VAR[,...]] [--passthru-env VAR[,...]] [--exclude-env VAR[,...]]
                 [--shell] [--no-shell] [--shlex] [--no-shlex] [--strip-colors]
                 [--no-strip-colors] [--quiet] [--verbose]
                 ...

Runs the given COMMAND with caching of stdout and stderr.

positional arguments:
  COMMAND

optional arguments:
  -h, --help            show this help message and exit
  --ttl DURATION, -t DURATION
                        Max length of time for which to cache command results. Format:
                        https://pypi.org/project/pytimeparse [default: 1d]
  --keep-failures, -F   Cache run results that exit non-zero. Does not cache these results by
                        default.
  --include-stdin, -i   Include stdin when computing cache key. Defaults to true if stdin is not a
                        TTY. If stdin is included, stdin will be read until EOF before executing
                        anything.
  --exclude-stdin, -I   Exclude stdin when computing cache key. Overrides -i.
  --include-env VAR[,...], -e VAR[,...]
                        Include named environment variable(s) when running command and when
                        computing cache key. Separate with commas or spaces. Escape separators
                        with shell-style quoting. May assign new value with VAR=value, or forward
                        existing value by simply naming VAR. Wildcards allowed when declaring
                        without assignment. Aggregates across all -e options.
  --passthru-env VAR[,...], -p VAR[,...]
                        Pass named environment variable(s) through to command without caching
                        them. Same format as -e. Any assignments override values from -e.
                        Aggregates across all -p options. [defaults: [EnvArg(envvar='HOME',
                        assigned_value=None), EnvArg(envvar='PATH', assigned_value=None),
                        EnvArg(envvar='TMPDIR', assigned_value=None)]]
  --exclude-env VAR[,...], -E VAR[,...]
                        Do not pass named environment variable(s) through to command, nor include
                        them when computing cache key. Same format as -e and -p except assignments
                        are disallowed. Aggregates across all -E options, and overrides -e and -p.
  --shell, -s           Pass COMMAND to $SHELL for execution. [default: False]
  --no-shell, -S        Do not pass COMMAND to $SHELL for execution. Overrides -s.
  --shlex, -l           Re-quote command line args before passing to $SHELL. Only used if shell is
                        true. [default: False]
  --no-shlex, -L        Do not re-quote command line args before passing to $SHELL. You may need
                        to embed additional quoting ensure the shell correctly interprets the
                        command.
  --strip-colors, -C    Strip ANSI escape sequences when printing cached output. Defaults to true
                        if stdout is not a TTY.
  --no-strip-colors, -c
                        Do not strip ANSI escape sequences when printing cached output.
  --quiet, -q           Set log level to warnings only.
  --verbose, -v         Set log level to debug.
```
<!--[[[end]]] (checksum: fea4eca1a3e4ffdc2bba5c0bf649536e)-->

## Prior Art

- bash - Memoizing/caching command line output - Unix & Linux Stack Exchange
  https://unix.stackexchange.com/questions/281479/memoizing-caching-command-line-output

- dimo414/bash-cache: Transparent caching layer for bash functions; particularly useful for functions invoked as part of your prompt.
  https://github.com/dimo414/bash-cache

- Run speficied (presumably expensive) command with specified arguments and cache result. If cache is fresh enough, don't run command again but return cached output.
  https://gist.github.com/akorn/51ee2fe7d36fa139723c851d87e56096

- sivann / runcached — Bitbucket
  https://bitbucket.org/sivann/runcached/src/master/
