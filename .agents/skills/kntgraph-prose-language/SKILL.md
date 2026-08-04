---
name: kntgraph-prose-language
description: Use when writing user-facing prose in kntgraph — docstrings, comments, module docs, ADRs, design notes, error messages, CLI copy. Covers the English-only rule, the do-not-translate-existing-PT-BR-content rule, and the CLI's PT-BR-domain-language exception (LICENÇA, CONFIGURAR in the knt CLI). Trigger keywords: docstring language, English, Portuguese, PT-BR, translate, CLI messages, LICENÇA, CONFIGURAR, knt CLI, user-facing copy.
---

# Prose language

All new prose (docstrings, comments, module docs, ADRs, design notes, error messages visible to the user) is in **English**.

Existing PT-BR content remains as historical records; **do translate it when possible**.

The exception is the CLI's user-facing messages (terminal output to Brazilian operators), which keep the PT-BR domain language where it carries semantic value (e.g. `LICENÇA`, `CONFIGURAR` in the `knt` CLI).
