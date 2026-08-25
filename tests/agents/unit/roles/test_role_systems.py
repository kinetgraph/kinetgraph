# SPDX-FileCopyrightText: 2026 kinetgraph
#
# SPDX-License-Identifier: Apache-2.0

"""
Role-system unit tests live elsewhere.

The previous contents of this file (15 tests covering
``ChatRoleSystem``, ``PlannerRoleSystem``,
``SummarizerRoleSystem``, ``PersonalizedRoleSystem``,
and ``RuleBasedChatSystem``) were deleted on 2026-08-26.

Reason: they relied on a ``ReactiveDispatcher._fold_with_filter``
monkey-patch that simulated memory hydration
(``project_memory``) — a projection that the **production
dispatcher does not invoke**. The tests were passing
against a simulated dispatcher that does not match
production behaviour; the role systems they exercised
do not function in production either (no ``SessionComponent``
ever reaches the ``AgentView``). See the file's git
history for the deleted code; the bug is tracked in
the roadmap under "Project Memory composition in
production dispatcher" (future ADR).
"""