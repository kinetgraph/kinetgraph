<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

name: Feature request
description: Suggest a feature for kntgraph
title: "[Feature]: "
labels: ["enhancement", "needs-triage"]
assignees: []

body:
  - type: markdown
    attributes:
      value: |
        Thanks for suggesting a feature. Before filing, please
        check existing issues and the [ADR index](../ADRs/)
        to see if the topic has been discussed before.

  - type: textarea
    id: problem
    attributes:
      label: Problem
      description: |
        What problem does this feature solve? Frame it as a
        user story if possible: "As a [user], I want [goal]
        so that [reason]."
      validations:
        required: true

  - type: textarea
    id: solution
    attributes:
      label: Proposed solution
      description: |
        What does the feature look like? Sketch the API,
        the new module, the new ADR. Code blocks are
        welcome.
    validations:
      required: true

  - type: textarea
    id: alternatives
    attributes:
      label: Alternatives considered
      description: |
        What other approaches did you consider? Why is the
        proposed solution the right one?
    validations:
      required: false

  - type: textarea
    id: breaking
    attributes:
      label: Breaking changes
      description: |
        Does this require a breaking change to the public API?
        If so, what is the migration path? (See AGENTS.md §2
        for the "no compat shims" rule.)
    validations:
      required: false

  - type: textarea
    id: context
    attributes:
      label: Additional context
      description: Anything else that might be relevant (links, prior art, …)
    validations:
        required: false
