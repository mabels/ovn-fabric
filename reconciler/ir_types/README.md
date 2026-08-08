# reconciler/ir_types/

Deliberately empty except for this file. The dataclasses this package
holds are generated, not hand-written — `ArkType`'s `.toJsonSchema()`
output, fed through `datamodel-code-generator` (build-time only, runs on
generator hardware), producing plain stdlib `dataclasses`. See
`docs/adr/0002-intermediate-representation.md`, "Reconciler/deployer
runtime" → "Type stability across the TypeScript/Python boundary".

Never hand-edit generated output here, and never commit it as if it
were source — it's ephemeral, regenerated on every build. Every
reconciler (`linux_net`, `netns`, `iptables`, `ovn`, `ovs`, ...) that
needs the shared IR node types imports from here once the generation
step exists; until then, reconcilers work with plain dicts (see
`linux_net/reconcile.py`) rather than a hand-faked stand-in for
generated code.
