const NOTION_DB_ID = '35487b36-74f1-800b-8dc0-f1ed0221d10c';
const EXAM_DB_ID = '7a6513a6-9179-4559-957a-a16e5f0260f8';
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

      const { question, options, correct, source, lecture, block, qIdx, reason, email } = body;
      if (!reason) return json({ error: 'reason_required' }, 400);

      if (!env.NOTION_TOKEN) return json({ error: 'no_token' }, 500);

      // Extract base reason (before any " — extra detail") for Issue Type mapping
      const baseReason = (reason || '').split(' — ')[0].trim();
      const issueType = ISSUE_TYPE_MAP[baseReason] || 'Other';

      const qLabel = (typeof qIdx === 'number') ? `Q${qIdx + 1}` : null;
      const context = [lecture, block, qLabel, source].filter(Boolean).join(' · ');

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

    if (url.pathname === '/api/exam-score') {
      if (request.method === 'OPTIONS') {
        return new Response(null, { status: 204, headers: corsHeaders() });
      }
      if (request.method !== 'POST') {
        return json({ error: 'method_not_allowed' }, 405);
      }

      let body;
      try { body = await request.json(); } catch { return json({ error: 'bad_request' }, 400); }

      // Anonymous submission: only these numeric/date fields are ever accepted or
      // forwarded to Notion. Request IP / user agent are never read or stored.
      const { examScorePct, appAccuracyPct, firstPassAccuracyPct, questionsAnswered, attemptsCount, saqAttempted, quizVersion } = body;

      const isPct = v => typeof v === 'number' && isFinite(v) && v >= 0 && v <= 100;
      const isCount = (v, max) => typeof v === 'number' && isFinite(v) && Number.isInteger(v) && v >= 0 && v <= max;
      const hasFirstPass = firstPassAccuracyPct !== null && firstPassAccuracyPct !== undefined;

      if (!isPct(examScorePct))                    return json({ error: 'invalid_exam_score' }, 400);
      if (!isPct(appAccuracyPct))                   return json({ error: 'invalid_app_accuracy' }, 400);
      if (hasFirstPass && !isPct(firstPassAccuracyPct)) return json({ error: 'invalid_first_pass_accuracy' }, 400);
      if (!isCount(questionsAnswered, 50000))       return json({ error: 'invalid_questions_answered' }, 400);
      if (!isCount(attemptsCount, 5000))            return json({ error: 'invalid_attempts_count' }, 400);
      if (!isCount(saqAttempted, 50000))            return json({ error: 'invalid_saq_attempted' }, 400);

      if (!env.NOTION_TOKEN) return json({ error: 'no_token' }, 500);

      const dateStr = new Date().toISOString().split('T')[0]; // date only — no timestamp precision
      const properties = {
        Name:                      { title: [{ text: { content: `Submission — ${dateStr}` } }] },
        'Exam Score (%)':          { number: examScorePct },
        'App Accuracy (%)':        { number: appAccuracyPct },
        'Questions Answered':      { number: questionsAnswered },
        'Attempts Count':          { number: attemptsCount },
        'SAQ Questions Attempted': { number: saqAttempted },
        Submitted:                 { date: { start: dateStr } },
        'App Version':             { rich_text: [{ text: { content: String(quizVersion || '').substring(0, 20) } }] },
      };
      if (hasFirstPass) properties['First-Pass Accuracy (%)'] = { number: firstPassAccuracyPct };

      const notionResp = await fetch('https://api.notion.com/v1/pages', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${env.NOTION_TOKEN.trim()}`,
          'Content-Type': 'application/json',
          'Notion-Version': NOTION_VERSION,
        },
        body: JSON.stringify({
          parent: { database_id: EXAM_DB_ID },
          properties,
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
