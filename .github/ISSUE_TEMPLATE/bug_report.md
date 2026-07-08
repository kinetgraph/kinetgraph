<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

name: Bug report
description: Report a bug in kntgraph
title: "[Bug]: "
labels: ["bug", "needs-triage"]
assignees: []

body:
  - type: markdown
    attributes:
      value: |
        Thanks for taking the time to file a bug report.

        Please do NOT report security vulnerabilities here. See
        [SECURITY.md](../SECURITY.md) for the private
        disclosure channel.

  - type: textarea
    id: description
    attributes:
      label: What happened?
      description: A clear and concise description of what the bug is.
      placeholder: |
        The `ReactiveDispatcher.dispatch_once()` returns 0 events
        when a `.requested` event has a missing `causation_id`.
    validations:
      required: true

  - type: textarea
    id: reproduction
    attributes:
      label: Steps to reproduce
      description: |
        Minimal script or snippet that reproduces the bug.
        Code blocks should be wrapped in triple backticks.
      placeholder: |
        ```python
        from kntgraph.core.event import Event
        from kntgraph.runner.reactive import ReactiveDispatcher

        dispatcher = ReactiveDispatcher(log=log, systems=[])
        # ... call dispatch_once ...
        ```
      render: python
    validations:
      required: true

  - type: textarea
    id: expected
    attributes:
      label: Expected behaviour
      description: What did you expect to happen?
    validations:
      required: true

  - type: textarea
    id: actual
    attributes:
      label: Actual behaviour
      description: What actually happened? Include the full error / traceback.
    validations:
      required: true

  - type: input
    id: version
    attributes:
      label: kntgraph version
      description: |
        Output of `python -c "import kntgraph; print(kntgraph.__version__)"`
      placeholder: "0.7.0"
    validations:
      required: true

  - type: input
    id: python
    attributes:
      label: Python version
      placeholder: "3.12.3"
    validations:
      required: true

  - type: input
    id: os
    attributes:
      label: Operating system
      placeholder: "Ubuntu 24.04"
    validations:
      required: true

  - type: textarea
    id: context
    attributes:
      label: Additional context
      description: Anything else that might be relevant (Redis version, environment vars, …)
    validations:
      required: false
