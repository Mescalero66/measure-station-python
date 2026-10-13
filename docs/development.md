# Development

The package runs on any OS. Hardware drivers are optional, and the `simulated`
sensor generates data so you can exercise the pipeline.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"     # Windows: .venv\Scripts\pip
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python -m measure_station   # data goes to ./data when not under systemd
```

To test without the MQ board, set `MQBOARD_PORT` to a pyserial URL such as
`socket://127.0.0.1:7777`, and serve simulated `$PMQB` sentences from that TCP port.
Or set `MQBOARD_ENABLED = False`.

Always run the station with `python -m measure_station`, never by file path. Running
a file inside the package would put that directory on `sys.path`, and then
`sysconfig.py` would shadow the standard library's `sysconfig` module.
