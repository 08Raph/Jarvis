"""Missions: a goal broken into a structured, inspectable plan that runs through the SAME validated, permission-checked executor as every other tool.

A mission is data. It cannot grant a permission, approve its own actions, call anything that is not a registered tool, or run code. Approving a plan
(`jarvis mission approve`) only lets the engine START it; every consequential step is still checked against the permission switches and still asks
for your confirmation when it runs. See docs/PROMPT9_REVIEW.md."""
