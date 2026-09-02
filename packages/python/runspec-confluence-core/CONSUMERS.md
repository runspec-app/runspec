# Consuming `runspec-confluence-core`

`runspec-confluence-core` is the dependency-light logic core behind the
`runspec-confluence` runnable. It has **no `runspec` dependency, no
`runspec.toml`, and no entry points**, so a private (e.g. Nexus-hosted) wrapper
package can depend on it, inject credentials in code, bake in defaults, and
surface its *own* runnables — without pulling in the published runnable or its
conventions.

This document is the contract for that wrapper author.

## Build a config in code (skip `config_from_env`)

`config_from_env` reads the `CONFLUENCE_*` environment family. A wrapper that
owns its own env-var namespace (or holds credentials some other way) should
construct `ConfluenceConfig` **directly** instead:

```python
from runspec_confluence_core import ConfluenceConfig, get_page

cfg = ConfluenceConfig(
    url="https://wiki.corp.example",
    cloud=False,                     # Server/Data Center
    personal_token=my_secret,        # or username=… + api_token=…
    space="DOCS",                    # optional baked-in default
)
page = get_page(cfg, page_id="12345", expand="body.storage")
```

Every operation takes a `ConfluenceConfig` as its first argument and returns
plain data, raising `ConfluenceError` on failure.

## Proxy and TLS-verification injection points

`ConfluenceConfig` carries the connection knobs `build_client` forwards to
`atlassian.Confluence` (and thus to `requests`):

- **`proxies: dict[str, str] | None`** — e.g. `{"http": url, "https": url}`.
- **`verify_ssl: bool | str`** — `True`/`False`, or a **path** to a CA bundle
  (for a private CA or a TLS-intercepting proxy). `resolve_verify()` normalises
  it into what `requests` expects.
- **`detect_verify() -> bool | str`** — best-effort platform trust-store
  detection a wrapper can call to pick a `verify_ssl` value. It injects
  `truststore` when installed, else returns a known system CA-bundle path, else
  `True`. It **never returns `False`**, so it will not silently disable TLS
  verification — opt out explicitly with `verify_ssl=False` if you must.

```python
cfg = ConfluenceConfig(url=…, cloud=False, personal_token=…,
                       verify_ssl=detect_verify(),
                       proxies={"https": "http://proxy.corp:8080"})
```

## The sparse-dict contract (`simplify_*`)

The `summarise.simplify_*` helpers (`simplify_page` / `_comment` / `_space` /
`_user` / `_attachment`) reduce a raw `atlassian-python-api` response to a small,
stable dict. The contract:

- **Best-effort and sparse** — a key appears **only** when the source has a
  meaningful value for it (empty/`None` are dropped). A partial API response
  never raises, so callers must treat every field as optional.
- **Both platforms** — the helpers read Cloud *and* Server/Data Center shapes
  (e.g. `simplify_user` finds email at the Cloud top level *or* Server/DC's
  `details.business.email`).
- **Depends on `expand`** — fields like a comment's `author`/`created` only
  appear when the fetch expanded the section that carries them (e.g.
  `get_comments(cfg, id, expand="body.view,history")`). Plumb `expand` through
  the read op to populate them.
- **Read vs. write conversion are not inverses** — `summarise.html_to_markdown`
  (Confluence HTML → Markdown, via `markdownify`) shapes reads;
  `mdconvert.markdown_to_storage` (Markdown → storage XHTML, via `markdown`)
  shapes writes.

## Import surface

Depend on `runspec-confluence-core` alone (not `runspec-confluence`) and import
from the package root:

```python
from runspec_confluence_core import (
    ConfluenceConfig, config_from_env, detect_verify, ConfluenceError,
    get_page, create_page, search, search_by_label, cql_from_text,
    list_spaces, get_space, get_space_pages, get_current_user,
    html_to_markdown, simplify_page, simplify_user,  # …and the rest
)
```
