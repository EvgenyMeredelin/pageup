"""Allow ``python3 -m pageup`` (Sigma deploy via PYTHONPATH + system python3).

Sigma's fapolicy blocks executing scripts from home directories, so operators
cannot run a ``pageup`` shell wrapper from ``~/projects``.  Instead they set
``PYTHONPATH`` to the unpacked ``pageup-sigma/lib/python3.13/site-packages``
tree and invoke the module directly — this file is the entry point Python
loads for that form.

Execution path:
    python3 -m pageup  →  __main__  →  pageup.cli.app  →  Typer  →  main()
    main() builds ParsingTask and delegates to runner.run() for Selenium.

On Fedora dev machines the same module works; ``uv sync`` also installs a
``.venv/bin/pageup`` console script that calls the same ``app`` object.
"""

from pageup.cli import app

if __name__ == "__main__":
    # Typer parses sys.argv and invokes the @app.command() handler (main).
    app()
