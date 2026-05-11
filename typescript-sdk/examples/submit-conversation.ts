// Sample: submit a transcript-only conversation for monitoring evaluation.
//
// Run with:
//   COVAL_API_KEY=... COVAL_AGENT_ID=... npm run example:submit-conversation
//
// For a production-style flow you'd swap `transcript` for `audio_url` pointing
// at a presigned URL of your call recording in S3 / GCS / Azure. The shape is
// otherwise identical.

import { CovalApiError, CovalClient } from '../src/index.js';

const apiKey = process.env.COVAL_API_KEY;
const agentId = process.env.COVAL_AGENT_ID;
if (!apiKey || !agentId) {
  console.error('Set COVAL_API_KEY and COVAL_AGENT_ID in the environment.');
  process.exit(1);
}

const coval = new CovalClient({ apiKey });

try {
  const result = await coval.conversations.submitConversation({
    covalConversationsAPISubmitConversationRequest: {
      agent_id: agentId,
      external_conversation_id: `sdk-smoke-${Date.now()}`,
      occurred_at: new Date(),
      transcript: [
        { role: 'agent', content: 'Hi, thanks for calling Yelp.' },
        { role: 'user', content: 'I need to update the email on my business account.' },
        { role: 'agent', content: 'Sure, I can help with that. Let me walk you through it.' },
      ],
      metadata: { source: 'coval-sdk-smoke-test' },
    },
  });

  console.log('Submitted:', JSON.stringify(result, null, 2));
} catch (err) {
  if (err instanceof CovalApiError) {
    console.error(`Coval API error ${err.status}: ${err.message}`);
    console.error(`  request_id: ${err.requestId ?? '-'}`);
    console.error(`  body: ${JSON.stringify(err.body)}`);
    process.exit(1);
  }
  throw err;
}
