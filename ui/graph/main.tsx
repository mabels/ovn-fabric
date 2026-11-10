/** @jsxImportSource npm:preact@10.27.2 */
// The topology graph page: fetches /graph.json (src/graph.ts, served by
// `serve-graph`, src/cli.ts) and draws it. Preact owns the toolbar and the
// detail panel, cytoscape owns the canvas. serve-graph bundles it for the
// browser when it starts.

import { render } from "npm:preact@10.27.2";
import { useEffect, useRef, useState } from "npm:preact@10.27.2/hooks";
// @deno-types="npm:@types/cytoscape@3.21.9"
import cytoscape from "npm:cytoscape@3.30.2";
import type { Graph, GraphNode, GraphNodeKind } from "../../src/graph.ts";

const KINDS: readonly GraphNodeKind[] = [
  "segment",
  "router",
  "netns",
  "app",
  "workload",
  "interface",
  "host",
];

interface Filters {
  readonly v6: boolean;
  readonly routes: boolean;
  readonly rules: boolean;
  readonly workloads: boolean;
  readonly hosts: boolean;
  readonly boxes: boolean;
}

const css = (name: string): string =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const keepAddr = (filters: Filters) => (addr: string): boolean =>
  filters.v6 || !addr.includes(":");

/** A vertex's text: its name, its addresses per leg, then its routes. */
function label(node: GraphNode, filters: Filters): string {
  const keep = keepAddr(filters);
  const lines = [node.name];
  for (const leg of node.legs) {
    const list = leg.addrs.filter(keep);
    if (list.length > 0) lines.push(`${leg.on}: ${list.join(" ")}`);
  }
  if (filters.routes) {
    const routes = node.routes.filter((r) => keep(r.dst));
    if (routes.length > 0) lines.push("──");
    for (const r of routes) lines.push(`${r.dst} → ${r.via}`);
  }
  if (filters.rules) {
    const rules = node.rules.filter((r) => filters.v6 || !r.includes("ipv6"));
    if (rules.length > 0) lines.push("── rules");
    lines.push(...rules);
  }
  return lines.join("\n");
}

function elements(
  graph: Graph,
  filters: Filters,
): cytoscape.ElementDefinition[] {
  const keep = keepAddr(filters);
  const shown = new Set(
    graph.nodes
      .filter((n) => filters.workloads || n.kind !== "workload")
      .filter((n) => filters.hosts || n.kind !== "host")
      .map((n) => n.id),
  );
  return [
    ...(filters.boxes ? graph.groups : []).map((g) => ({
      data: { id: g.id, kind: "group", label: g.name },
    })),
    ...graph.nodes.filter((n) => shown.has(n.id)).map((n) => ({
      data: {
        id: n.id,
        kind: n.kind,
        label: label(n, filters),
        ...(filters.boxes && n.group ? { parent: n.group } : {}),
      },
    })),
    ...graph.edges
      .filter((e) => shown.has(e.source) && shown.has(e.target))
      .map((e) => ({
        data: {
          id: e.id,
          source: e.source,
          target: e.target,
          role: e.role,
          label: e.addrs.filter(keep).join(" "),
        },
      })),
  ];
}

function style(): cytoscape.Stylesheet[] {
  const kind = (k: GraphNodeKind | "group", shape: string) => ({
    selector: `node[kind="${k}"]`,
    style: {
      "background-color": css(`--${k}`),
      "border-color": css(`--${k}-b`),
      shape,
    },
  });
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "text-wrap": "wrap",
        "text-valign": "center",
        "text-halign": "center",
        "font-family": "ui-monospace, Menlo, monospace",
        "font-size": 9,
        color: css("--fg"),
        width: "label",
        height: "label",
        padding: "8px",
        "border-width": 1.5,
      },
    },
    kind("segment", "ellipse"),
    kind("router", "round-rectangle"),
    kind("netns", "round-rectangle"),
    kind("app", "round-hexagon"),
    kind("workload", "round-tag"),
    kind("interface", "cut-rectangle"),
    kind("host", "barrel"),
    kind("group", "round-rectangle"),
    {
      selector: 'node[kind="group"]',
      style: {
        "text-valign": "top",
        "text-halign": "center",
        "font-size": 12,
        "font-weight": 700,
        "font-family": "system-ui, sans-serif",
        "background-opacity": 0.35,
        "border-style": "dashed",
        padding: "18px",
        "text-margin-y": -4,
      },
    },
    {
      selector: 'node[kind="host"]',
      style: { "font-size": 11, "font-weight": 600 },
    },
    {
      selector: 'node[kind="segment"]',
      style: { "font-size": 11, "font-weight": 600 },
    },
    {
      selector: "edge",
      style: {
        width: 1.2,
        "line-color": css("--edge"),
        "curve-style": "bezier",
        label: "data(label)",
        "font-size": 8,
        color: css("--muted"),
        "text-rotation": "autorotate",
        "text-background-color": css("--bg"),
        "text-background-opacity": 0.8,
      },
    },
    {
      selector: 'edge[role="binding"]',
      style: { "line-style": "dashed", width: 1.6 },
    },
    {
      selector: 'edge[role="host"]',
      style: { "line-style": "dotted", "line-color": css("--host-b") },
    },
    {
      selector: 'edge[role="runs"]',
      style: { "line-style": "dashed", "line-color": css("--app-b") },
    },
    {
      selector: ".hit",
      style: { "border-width": 4, "border-color": css("--hit") },
    },
    { selector: ":selected", style: { "border-width": 4 } },
  ] as cytoscape.Stylesheet[];
}

function GraphView(props: {
  graph: Graph;
  filters: Filters;
  query: string;
  fitSignal: number;
  onSelect: (id: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const cy = useRef<cytoscape.Core | null>(null);

  // Drawn anew when the graph or a filter changes; vertices that were
  // there keep where they were.
  useEffect(() => {
    const previous = cy.current;
    const positions = new Map<string, cytoscape.Position>();
    previous?.nodes().forEach((n) => {
      positions.set(n.id(), { ...n.position() });
    });
    previous?.destroy();
    const els = elements(props.graph, props.filters);
    const known = els.every((e) =>
      e.data.source !== undefined || e.data["kind"] === "group" ||
      positions.has(String(e.data.id))
    );
    const instance = cytoscape({
      container: container.current,
      elements: els,
      style: style(),
      wheelSensitivity: 0.3,
      layout: known
        ? { name: "preset", positions: Object.fromEntries(positions) }
        : {
          name: "cose",
          animate: false,
          nodeRepulsion: () => 900000,
          idealEdgeLength: () => 140,
          nodeDimensionsIncludeLabels: true,
          padding: 30,
        } as cytoscape.LayoutOptions,
    });
    instance.on("tap", "node", (ev) => props.onSelect(ev.target.id()));
    cy.current = instance;
  }, [props.graph, props.filters]);

  useEffect(() => () => cy.current?.destroy(), []);

  useEffect(() => {
    const instance = cy.current;
    if (instance === null) return;
    instance.nodes().removeClass("hit");
    const q = props.query.trim().toLowerCase();
    if (q === "") return;
    for (const node of props.graph.nodes) {
      if (JSON.stringify(node).toLowerCase().includes(q)) {
        instance.getElementById(node.id).addClass("hit");
      }
    }
  }, [props.query, props.graph, props.filters]);

  useEffect(() => {
    if (props.fitSignal > 0) cy.current?.fit(undefined, 30);
  }, [props.fitSignal]);

  return <div class="canvas" ref={container} />;
}

function Detail(props: { node: GraphNode | undefined }) {
  const node = props.node;
  if (node === undefined) {
    return (
      <aside>
        <p>Click a vertex for its addresses and routes.</p>
      </aside>
    );
  }
  return (
    <aside>
      <h2>{node.name}</h2>
      <div class="kind">{node.kind}</div>
      {node.legs.length > 0 && (
        <>
          <h3>Legs</h3>
          <table>
            {node.legs.map((leg) => (
              <tr key={leg.on + (leg.name ?? "")}>
                <td>
                  {leg.on}
                  {leg.name && (
                    <>
                      <br />
                      <small>{leg.name}</small>
                    </>
                  )}
                </td>
                <td>{leg.addrs.map((a) => <div key={a}>{a}</div>)}</td>
              </tr>
            ))}
          </table>
        </>
      )}
      {node.routes.length > 0 && (
        <>
          <h3>Routes</h3>
          <table>
            {node.routes.map((r) => (
              <tr key={r.dst + r.via + (r.domain ?? "")}>
                <td>{r.dst}</td>
                <td>{r.via}</td>
                <td>{r.domain ?? ""} {r.metric ?? ""}</td>
              </tr>
            ))}
          </table>
        </>
      )}
      {node.rules.length > 0 && (
        <>
          <h3>Security groups</h3>
          <table>
            {node.rules.map((rule) => (
              <tr key={rule}>
                <td>{rule}</td>
              </tr>
            ))}
          </table>
        </>
      )}
      {node.notes.length > 0 && (
        <>
          <h3>Notes</h3>
          <table>
            {node.notes.map((note) => (
              <tr>
                <td>{note}</td>
              </tr>
            ))}
          </table>
        </>
      )}
    </aside>
  );
}

function Toggle(props: {
  label: string;
  value: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <label>
      <input
        type="checkbox"
        checked={props.value}
        onChange={(e) => props.onChange((e.target as HTMLInputElement).checked)}
      />
      {props.label}
    </label>
  );
}

function App() {
  const [graph, setGraph] = useState<Graph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState<Filters>({
    v6: false,
    routes: true,
    rules: true,
    workloads: true,
    hosts: true,
    boxes: true,
  });
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [fitSignal, setFitSignal] = useState(0);

  async function load() {
    setLoading(true);
    try {
      const res = await fetch("graph.json", { cache: "no-store" });
      const body = await res.json();
      if (!res.ok) throw new Error(body.error ?? res.statusText);
      setGraph(body as Graph);
      setError(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load();
  }, []);

  const set = (key: keyof Filters) => (value: boolean) =>
    setFilters({ ...filters, [key]: value });

  return (
    <>
      <header>
        <h1>{graph?.name ?? "Topology"}</h1>
        <Toggle label="IPv6" value={filters.v6} onChange={set("v6")} />
        <Toggle
          label="routes"
          value={filters.routes}
          onChange={set("routes")}
        />
        <Toggle label="rules" value={filters.rules} onChange={set("rules")} />
        <Toggle label="hosts" value={filters.hosts} onChange={set("hosts")} />
        <Toggle label="boxes" value={filters.boxes} onChange={set("boxes")} />
        <Toggle
          label="workloads"
          value={filters.workloads}
          onChange={set("workloads")}
        />
        <input
          type="search"
          placeholder="find name or address"
          value={query}
          onInput={(e) => setQuery((e.target as HTMLInputElement).value)}
        />
        <button type="button" onClick={load}>Reload</button>
        <button type="button" onClick={() => setFitSignal(fitSignal + 1)}>
          Fit
        </button>
        <div class="legend">
          {KINDS.map((k) => (
            <span
              key={k}
              style={{
                background: `var(--${k})`,
                borderColor: `var(--${k}-b)`,
              }}
            >
              {k}
            </span>
          ))}
        </div>
        <span class={error ? "status error" : "status"}>
          {loading
            ? "loading…"
            : error
            ? `error: ${error}`
            : graph
            ? `${graph.nodes.length} vertices, ${graph.edges.length} edges`
            : ""}
        </span>
      </header>
      <main>
        {graph && (
          <GraphView
            graph={graph}
            filters={filters}
            query={query}
            fitSignal={fitSignal}
            onSelect={setSelected}
          />
        )}
        <Detail node={graph?.nodes.find((n) => n.id === selected)} />
      </main>
    </>
  );
}

render(<App />, document.getElementById("app")!);
