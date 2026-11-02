"""primate — PRIMitive autoMATE: learns, on a real system, what each
deployer primitive leaves in RawIR, and writes it down as templates
(ADR 0008).

    python3 -m primate learn [--sample NAME …] > templates.json

For every sample call (samples.py) it collects RawIR, applies the
primitive, collects again, deletes it and collects once more — twice,
with different marker values — and turns the difference into a template:
the nodes the primitive adds and the fields it changes on nodes that were
there, with its parameters as placeholders (learn.py). It changes the
system on purpose: run it on a disposable test system as root, never on
a production host. Everything it creates carries a `pm` marker name and
is removed again.
"""
