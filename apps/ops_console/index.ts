const server = Bun.serve({
  port: 3000,
  fetch() {
    return new Response(JSON.stringify({
      app: "ops_console",
      status: "ok",
      queues: ["triage", "missed-dose", "refill-approval"]
    }), {
      headers: { "content-type": "application/json" },
    });
  },
});

console.log(`ops_console listening on http://localhost:${server.port}`);
