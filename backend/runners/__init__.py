"""Execution engines for SLEP. One module per tool; each exposes a
`launch(run_id)` that runs to completion (blocking) and is meant to be called on
a background thread. Ansible is first; terraform and salt follow the same shape.
"""
