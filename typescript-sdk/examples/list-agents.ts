// Sample: list all agents in the org, iterating across pages.
//
// Run with:
//   COVAL_API_KEY=... npm run example:list-agents

import { CovalClient, paginate } from '../src/index.js';

const apiKey = process.env.COVAL_API_KEY;
if (!apiKey) {
  console.error('Set COVAL_API_KEY in the environment.');
  process.exit(1);
}

const coval = new CovalClient({ apiKey });

const allAgents: Array<{ id: string; display_name?: string; model_type?: string }> = [];

for await (const agent of paginate({
  fetchPage: (pageToken) => coval.agents.listAgents({ pageToken, pageSize: 50 }),
  items: (page) => page.agents,
  nextToken: (page) => page.next_page_token,
})) {
  allAgents.push(agent as { id: string; display_name?: string; model_type?: string });
}

console.log(`Found ${allAgents.length} agent${allAgents.length === 1 ? '' : 's'}.\n`);
for (const a of allAgents) {
  console.log(`  ${a.id}  ${a.model_type ?? '-'}  ${a.display_name ?? '(unnamed)'}`);
}
