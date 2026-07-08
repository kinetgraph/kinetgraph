<!--
SPDX-FileCopyrightText: 2026 kinetgraph

SPDX-License-Identifier: Apache-2.0
-->

name: Question
description: Ask a question about kntgraph
title: "[Question]: "
labels: ["question", "needs-triage"]
assignees: []

body:
  - type: textarea
    id: question
    attributes:
      label: Your question
      description: |
        What would you like to know? Be as specific as
        possible — link to the relevant docs/ADR/code.
      validations:
        required: true

  - type: textarea
    id: context
    attributes:
      label: What have you tried so far?
      description: |
        If applicable, describe the research you have done
        (which docs, which ADRs, which examples).
    validations:
      required: false
