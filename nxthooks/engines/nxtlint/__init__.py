"""NxtLint — advisory Python lint for agent-written code.

Pairs with NxtGrep the way advice pairs with a gate. NxtGrep inspects a tool
call's payload before it lands and refuses secrets; NxtLint reads the file
after it lands and tells the agent what it broke. The split is the phase:
nothing here can refuse, because a linter that refuses fires on a file an
agent is two Edits into writing, and a half-written function is not a defect.
"""
