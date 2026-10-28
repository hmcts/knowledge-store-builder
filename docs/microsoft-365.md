# Microsoft 365 Copilot and the hosted agent

This page is for the people deciding whether a store may be asked about from
Microsoft 365, and for those building the hosted agent. It expands the short
[A hosted agent](mcp-server.md#a-hosted-agent) and
[Microsoft 365 Copilot](mcp-server.md#microsoft-365-copilot) sections of the
server reference.

## A hosted agent

A hosted agent, built on the Claude Agent SDK and run on Amazon Bedrock, is being designed separately. It uses this same server, launched as a stdio child process of the agent, with the same tools and the same guidance.

## Microsoft 365 Copilot

There is no route from Microsoft 365 Copilot in version 1. Microsoft 365 Copilot and Copilot Studio run in Microsoft's cloud and reach MCP servers over Streamable HTTP only ([Connect your agent to an existing MCP server](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-add-existing-server-to-agent)). They cannot launch a local stdio process, which is the only transport this server speaks.

Two routes are possible later.

| Route | How it works | What changes | Status |
| --- | --- | --- | --- |
| **B. Through the hosted agent** (intended) | A Copilot Studio agent action, connector or Teams bot passes the question to the hosted agent's endpoint. Claude reasons there, with this server as its stdio child, and returns a cited answer. | Nothing in this server. | In design, with the hosted agent above. |
| **A. Direct** | Copilot Studio adds this server as an MCP tool source. | A hosted Streamable HTTP transport behind Entra ID, reachable from Microsoft's side. Microsoft's model does the reasoning, not Claude, so answer quality measured on Claude does not carry over, and whether Copilot Studio passes on the server's guidance is unverified. | Not planned for version 1. Demand for it is what would justify an HTTP transport. |

**Choosing.** Take route B to keep Claude's reasoning and the answer quality the store was measured on. Take route A only if answers reasoned by Microsoft's model are acceptable for your store.

**Building a route does not wait on these decisions; enabling it for a store does.** Whether Microsoft 365 Copilot may be used at all is the organisation's platform decision. Once a route exists, each store and each deployment that switches it on works through two decisions for that store:

- **Data governance, in both directions.**
  - *Into Microsoft 365:* an answer carries store content there, including repository paths, ticket ids and quoted commit text.
  - *Out of Microsoft 365:* in route B, the user's question leaves Microsoft 365 for the hosted agent's endpoint in another cloud. A question can carry sensitive text.
  - *What is kept:* what the hosted agent logs of each question and answer, and for how long.

  The store's owner confirms all three for the store's classification, and names who approves them, before the route is enabled for that store.
- **Who may ask.** The Entra group allowed to ask should match the people who can read the store's repository on GitHub. A wider group would widen access to the store.
