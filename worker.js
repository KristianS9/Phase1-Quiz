const NOTION_DB_ID = '35487b36-74f1-800b-8dc0-f1ed0221d10c';
const NOTION_VERSION = '2022-06-28';

// Map quiz reason labels to Issue Type select options in the DB
const ISSUE_TYPE_MAP = {
  'Incorrect answer':               'Incorrect Answer',
  'Misleading or ambiguous wording':'Unclear Wording',
  'Outdated information':           'Outdated Information',
  'Poor distractor quality':        'Other',
  'Other':                          'Other',
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === '/api/report') {
      if (request.method === 'OPTIONS') {
        return new Response(null, { status: 204, headers: corsHeaders() });
      }
      if (request.method !== 'POST') {
        return json({ error: 'method_not_allowed' }, 405);
      }

      let body;
      try { body = await request.json(); } catch { return json({ error: 'bad_request' }, 400); }

      const { question, options, correct, source, lecture, block, reason, email } = body;
      if (!reason) return json({ error: 'reason_required' }, 400);

      if (!env.NOTION_TOKEN) return json({ error: 'no_token' }, 500);

      // Extract base reason (before any " — extra detail") for Issue Type mapping
      const baseReason = (reason || '').split(' — ')[0].trim();
      const issueType = ISSUE_TYPE_MAP[baseReason] || 'Other';

      const context = [lecture, block, source].filter(Boolean).join(' · ');

      const notionResp = await fetch('https://api.notion.com/v1/pages', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.NOTION_TOKEN.trim()}`,
          'Content-Type': 'application/json',
          'Notion-Version': NOTION_VERSION,
        },
        body: JSON.stringify({
          parent: { database_id: NOTION_DB_ID },
          properties: {
            Name:            { title: [{ text: { content: (question || '').substring(0, 200) } }] },
            'Issue Type':    { select: { name: issueType } },
            Description:     { rich_text: [{ text: { content: (reason || '').substring(0, 500) } }] },
            'Question ID':   { rich_text: [{ text: { content: context.substring(0, 200) } }] },
            'Date Reported': { date: { start: new Date().toISOString().split('T')[0] } },
            'Reporter Email':{ email: (email && email.includes('@')) ? email : null },
            Status:          { status: { name: 'New' } },
          },
          children: [
            heading('Question'),
            paragraph(question || ''),
            heading('Options'),
            ...(options || []).map((opt, i) => {
              const letter = 'ABCDE'[i];
              return paragraph(`${letter}: ${opt}${letter === correct ? '  ✓' : ''}`);
            }),
          ],
        }),
      });

      if (!notionResp.ok) {
        console.error('Notion error', notionResp.status, await notionResp.text());
        return json({ error: 'notion_error' }, 502);
      }

      return json({ ok: true }, 200);
    }

    // All other requests: serve static assets
    return env.ASSETS.fetch(request);
  },
};

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders() },
  });
}

function heading(text) {
  return { object: 'block', type: 'heading_3', heading_3: { rich_text: [{ type: 'text', text: { content: text } }] } };
}

function paragraph(text) {
  return { object: 'block', type: 'paragraph', paragraph: { rich_text: [{ type: 'text', text: { content: String(text) } }] } };
}

function corsHeaders() {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
}
