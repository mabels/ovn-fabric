"""The primitives: typed objects of the host — netns, links, addresses,
routes, OVS bridges and ports, OVN northbound objects, docker containers,
files — each with its create and delete (fabric/lifecycle.py). A unit of
work is a list of them (fabric/units.py); primate learns what each one
does to RawIR (fabric/rawir_learned.py, ADR 0008).
"""
