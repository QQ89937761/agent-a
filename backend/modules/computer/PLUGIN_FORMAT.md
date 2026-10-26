# XiaoA isolated plugin format

An executable plugin is a GitHub repository (or `/tree/ref/subdir`) containing
`plugin.json` and the declared Python or Node entry file. XiaoA never runs
README installation commands or repository lifecycle scripts.

```json
{
  "name": "weather_lookup",
  "description": "Look up weather for one city.",
  "runtime": "python",
  "entry": "main.py",
  "parameters": {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
    "additionalProperties": false
  },
  "dependencies": ["httpx==0.28.1"],
  "test_args": {"city": "Beijing"},
  "side_effect": false,
  "timeout": 30
}
```

Python dependencies must use exact `name==version` pins and must have binary
wheels. Node dependencies use an object of exact versions, for example
`{"lodash":"4.17.21"}`; npm runs with `--ignore-scripts --omit=dev`.

The entry program reads one JSON object from stdin and writes exactly one JSON
value to stdout. Diagnostics go to stderr. The process receives a minimal
environment without XiaoA model, cloud, GitHub, Feishu, or voice keys.

Limits: 200 source files, 20 MB source, 1 MB input, 1 MB stdout, 20 KB stderr,
and a 300-second execution timeout. Each version has its own environment;
updates retain earlier versions for rollback.

Development mode uses the installed Python/Node runtimes. The portable full
edition uses separate runtimes under `resources/plugin-runtime/python` and
`resources/plugin-runtime/node`; the frozen backend executable is never used as
a Python interpreter.
