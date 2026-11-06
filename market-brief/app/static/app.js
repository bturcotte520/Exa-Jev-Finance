'use strict';

const $ = (id) => document.getElementById(id);
const activeStatuses = new Set(['queued', 'searching', 'judging']);
const sourceTabs = ['Source', 'Exa request', 'Jev request', 'Jev response', 'Routing', 'Usage/timing'];
const quoteTabs = ['Quote request', 'Quote source'];
const state = {config: null, quotes: [], ticker: null, run: null, replay: false, busy: false,
  loading: false, epoch: 0, timer: null, sourceId: null, inspector: null, tab: '', queryId: null, reviewNotes: {}};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function text(value) {
  return value === undefined || value === null ? 'Not reported' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
}
function date(value) {
  if (!value) return 'Not reported';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? text(value) : `${parsed.toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'})} (local)`;
}
function label(value) { return text(value).replaceAll('_', ' '); }
function choiceLabel(value) {
  return ({company: 'Directly about this company', business_connection: 'Connected customer, supplier or competitor',
    sector_macro: 'Relevant sector, policy or economic news', unrelated: 'Not relevant', unclear: 'Relevance unclear',
    concrete_development: 'Reports a concrete development', price_recap_only: 'Price movement only',
    commentary: 'Opinion or prediction', insufficient: 'Not enough information'})[value] || 'Not classified';
}
function confidence(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? `${(value * 100).toFixed(1)}%` : 'Not reported';
}
function showClassifications(parent, source) {
  const tags = node('div', null, 'labels');
  for (const [name, title] of [['scope', 'Relevance'], ['substance', 'Content']]) {
    const answer = source.judgment?.answers?.[name];
    tags.append(node('span', `${title}: ${choiceLabel(answer?.choice ?? answer)} · Confidence: ${confidence(answer?.confidence)}`, 'tag'));
  }
  parent.append(tags);
}
function destination(value) {
  return ({company: 'Company developments', context: 'Broader context', commentary: 'Commentary',
    review: 'Needs review', out: 'Excluded'})[value] || 'Not yet routed';
}
function progress(run) {
  const sources = run.sources || [];
  const classified = sources.filter(source => source.judgment?.valid).length;
  if (run.kind === 'reclassification') {
    if (run.status === 'queued') return 'Waiting to send the saved evidence to Jev. No prices or searches will be refreshed.';
    if (run.status === 'judging') return `Jev has reclassified ${classified} of ${sources.length} saved sources.`;
    if (run.status === 'complete') return `Reclassification ready. Jev classified ${classified} saved sources; the original run is unchanged.`;
  }
  const descriptions = {
    queued: 'Waiting to start the three Exa searches.',
    searching: 'Exa is searching for company news, related businesses, and sector developments.',
    judging: `Exa found ${sources.length} sources. Jev has classified ${classified} of ${sources.length}.`,
    complete: `Draft ready. Exa found ${sources.length} sources; Jev classified ${classified}.`,
    partial: 'Draft ready with gaps. Some searches or classifications failed; check the warnings and review queue.',
    no_sources: 'Exa returned no sources for this search window.',
    failed: 'Research failed. Check the warnings below.',
    interrupted: 'Research was interrupted. Saved results are available; no requests were restarted.'
  };
  if (run.busy) return 'Getting more passages from Exa, then asking Jev to classify that source again.';
  return descriptions[run.status] || run.stage || 'Status unavailable';
}
function companyName(company) { return company?.company || company?.ticker || 'Company'; }
function signed(value, suffix = '') {
  return typeof value === 'number' && Number.isFinite(value) ? `${value > 0 ? '+' : ''}${value.toFixed(2)}${suffix}` : 'Not reported';
}
function price(quote) {
  const fields = quote?.fields;
  if (!quote?.valid || typeof fields?.price !== 'number' || !Number.isFinite(fields.price)) return 'Unavailable';
  return `${fields.currency || 'Currency not reported'} ${fields.price.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
}
function error(message) {
  $('error').textContent = message || '';
  $('error').hidden = !message;
}
async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {headers: {'Content-Type': 'application/json'}, ...options});
  let data;
  try { data = await response.json(); } catch { throw new Error(`Server returned an unreadable response (${response.status}).`); }
  if (!response.ok) throw new Error(text(data.detail || data.error || `Request failed (${response.status}).`));
  return data;
}
function button(title, handler, className = 'quiet') {
  const element = node('button', title, className);
  element.type = 'button';
  element.addEventListener('click', handler);
  return element;
}
function link(url, title = 'Open source') {
  try {
    const parsed = new URL(url);
    if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error('Unsafe protocol');
    const element = node('a', title);
    element.href = parsed.href;
    element.target = '_blank';
    element.rel = 'noopener noreferrer';
    return element;
  } catch { return node('span', 'Source link unavailable', 'helper'); }
}
function json(parent, title, value, open = false) {
  const details = node('details');
  details.open = open;
  details.append(node('summary', title), node('pre', text(value)));
  parent.append(details);
}
function facts(parent, values) {
  const list = node('dl');
  for (const [name, value] of Object.entries(values)) list.append(node('dt', name), node('dd', text(value)));
  parent.append(list);
}
function selectedQuote() { return state.quotes.find((quote) => quote.ticker === state.ticker); }
function priorDate(quote, reference = new Date()) {
  const stamp = new Date(quote?.fields?.quote_time);
  return quote?.fields?.prior_date || (!Number.isNaN(stamp.getTime()) && stamp.toISOString().slice(0, 10) < new Date(reference).toISOString().slice(0, 10));
}
function running() { return !!state.run && activeStatuses.has(state.run.status); }
function readOnly() { return state.replay || !!state.run?.readonly; }
function actionReason(expansion = false) {
  if (readOnly()) return 'Saved research is read-only.';
  if (running()) return 'Research in progress.';
  if (state.busy || state.loading || state.run?.busy) return 'An action is in progress.';
  if (expansion && !state.config?.credentials?.research) return 'Get more context requires configured Exa and Jev credentials.';
  if (expansion && (state.run?.sources || []).filter((source) => source.expansion != null).length >= 2) return 'The two-source context expansion limit has been reached.';
  return '';
}
function renderMarket() {
  const config = state.config;
  $('check-prices').disabled = !config?.credentials?.prices || state.busy || state.loading;
  $('add-ticker').disabled = $('check-prices').disabled;
  $('ticker-input').disabled = $('check-prices').disabled;
  $('ticker-reason').textContent = !config?.credentials?.prices
    ? 'Add ticker disabled: Financial Datasets price credentials are required.'
    : '';
  $('check-prices').textContent = state.busy && !state.run ? 'Working...' : 'Refresh prices';
  $('price-reason').textContent = !config ? 'Configuration unavailable. Reload to retry.' : !config.credentials.prices
    ? 'Price refresh disabled: Financial Datasets credentials (fin_data) are not configured.'
    : '';
  const list = $('watchlist');
  list.replaceChildren();
  for (const company of config?.watchlist || []) {
    const quote = state.quotes.find((item) => item.ticker === company.ticker);
    const stock = button('', () => { state.ticker = company.ticker; renderMarket(); }, 'stock');
    stock.setAttribute('aria-pressed', String(state.ticker === company.ticker));
    stock.setAttribute('aria-label', `Select ${company.company}, ${company.ticker}`);
    const top = node('div', null, 'stock-top');
    top.append(node('span', company.ticker, 'stock-ticker'), node('span', company.currency, 'helper'));
    const value = node('div', null, 'stock-value');
    value.append(node('span', quote ? price(quote) : 'Awaiting quote', 'stock-price'));
    if (quote?.valid) value.append(node('span', signed(quote.fields?.day_change_percent, '%'), `stock-change ${quote.fields?.day_change_percent < 0 ? 'negative' : 'positive'}`));
    stock.append(top, node('span', company.company, 'stock-name'), value);
    if (quote) stock.append(node('span', !quote.valid ? `Quote failed: ${text(quote.error)}`
      : `${priorDate(quote) ? 'Prior-date quote · ' : ''}${date(quote.fields?.quote_time)}`, 'helper'));
    list.append(stock);
  }
  const quote = selectedQuote();
  const summary = $('quote-summary');
  summary.replaceChildren();
  if (quote) {
    summary.append(node('strong', `${quote.ticker} · ${quote.valid ? price(quote) : 'Invalid quote'}`));
    if (quote.error) summary.append(node('p', text(quote.error), 'source-error'));
  } else summary.append(node('p', 'Add a ticker or refresh prices.'));
  $('inspect-quote').disabled = !quote;
  const reason = !config?.credentials?.research ? 'Research disabled: Exa (exa_key) and Jev (jev_key) credentials are required.'
    : !quote?.valid || !quote.id ? 'A valid quote is required.'
    : state.busy || state.loading ? 'An action is in progress.' : running() && !readOnly() ? 'Research is already running. Follow its progress below.' : '';
  $('research').disabled = !!reason;
  $('research').textContent = state.ticker ? `Research ${state.ticker}` : 'Research company';
  $('research-reason').textContent = reason;
  $('research').title = reason || 'Search with Exa and classify with Jev. Uses API credits.';
}
function passages(source) {
  return (source.passages || []).map((passage) => typeof passage === 'string' ? passage : text(passage.text ?? passage));
}
function showPassages(parent, source, full = false) {
  const items = passages(source);
  if (!items.length) { parent.append(node('p', 'No usable passages captured. Needs review.', 'helper')); return; }
  function excerpt(passage, index) {
    const block = node('blockquote', null, 'markdown-passage');
    // Only the server's HTML-disabled Markdown renderer supplies this display field.
    if (typeof source.passages_html?.[index] === 'string') block.innerHTML = source.passages_html[index];
    else block.textContent = passage;
    return block;
  }
  const blocks = items.map(excerpt);
  if (full) { parent.append(...blocks); return; }
  const plain = blocks.map(block => block.textContent).join(' ').replace(/\s+/g, ' ').trim();
  parent.append(node('blockquote', plain.length > 280 ? `${plain.slice(0, 277).trimEnd()}...` : plain, 'excerpt-preview'));
  const rest = node('details', null, 'full-excerpt');
  rest.append(node('summary', `Read full excerpt · ${items.length} passage${items.length === 1 ? '' : 's'}`), ...blocks);
  parent.append(rest);
}
function sourceDates(source) {
  const check = source.date_check || {};
  const timing = {before_quote: 'Before quote', after_quote: 'After quote', at_quote: 'At quote time'}[check.timing];
  return [source.published_date ? `Published ${date(source.published_date)}` : 'Publication date unknown',
    check.outside_window ? 'Outside research window' : '', check.uncertain ? 'Timing uncertain' : timing].filter(Boolean).join(' · ');
}
function sourceButton(id) { return button(id, () => inspectSource(id), 'source-id'); }
function included(id) {
  return Object.values(state.run?.briefing?.sections || {}).some((ids) => Array.isArray(ids) && ids.includes(id));
}
function showInBriefing(id) {
  const card = document.getElementById(`brief-${id}`);
  if (!card) return;
  document.querySelectorAll('.brief-card.highlight').forEach((item) => item.classList.remove('highlight'));
  card.classList.add('highlight');
  card.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'center'});
  card.focus({preventScroll: true});
}
function renderEvidence() {
  const sources = state.run.sources || [];
  $('source-count').textContent = `${sources.length} source${sources.length === 1 ? '' : 's'}`;
  const container = $('evidence');
  const openSources = new Set([...container.querySelectorAll('.source-context[open]')].map(el => el.dataset.source));
  container.replaceChildren();
  if (sources.length) {
    const headings = node('div', null, 'source-columns source-column-headings');
    headings.append(node('span', 'Source'), node('span', 'Jev relevance'), node('span', 'Jev content'));
    container.append(headings);
  }
  if (!sources.length) {
    const empty = node('div', null, 'empty-state');
    empty.append(node('p', running() ? 'Retrieving sources...' : state.run.status === 'no_sources' ? 'No sources found.' : 'No evidence available.'));
    container.append(empty);
  }
  for (const source of sources) {
    const row = node('article', null, `evidence-card source-columns${state.sourceId === source.id ? ' selected' : ''}`);
    row.id = `evidence-${source.id}`;
    const main = node('div', null, 'source-main');
    main.append(node('h3', source.title || 'Untitled source', 'source-title'), link(source.url, 'Open source'));
    const card = node('details', null, 'source-context');
    card.dataset.source = source.id;
    card.open = openSources.has(source.id);
    card.append(node('summary', 'Context & controls'));
    const top = node('div', null, 'source-top');
    top.append(sourceButton(source.id), node('span', source.domain, 'source-domain'));
    card.append(top);
    card.append(node('p', sourceDates(source), 'source-meta timing'));
    showPassages(card, source, true);
    card.append(node('p', `${source.review ? 'Your decision' : 'Placement'}: ${destination(source.route?.final)}`, 'route-reason'));
    if (source.judgment?.error) card.append(node('p', `Classification error: ${text(source.judgment.error)}`, 'source-error'));
    if (source.expansion?.error) card.append(node('p', `Context expansion error: ${text(source.expansion.error)}`, 'source-error'));
    const actions = node('div', null, 'card-actions');
    actions.append(button('Inspect evidence', () => inspectSource(source.id)));
    if (included(source.id)) actions.append(button('Show in briefing', () => showInBriefing(source.id)));
    const expand = button('Get more context', () => mutateSource(source.id, 'expand'));
    const expandReason = actionReason(true) || (source.expansion ? 'Context expansion has already been attempted for this source.' : '');
    expand.disabled = !!expandReason;
    expand.title = expandReason || 'Exa retrieves more passages from this page, then Jev classifies the source again. Uses your API credits.';
    actions.append(expand);
    card.append(actions);
    const noteId = `note-${source.id}`;
    const noteLabel = node('label', 'Your review note', 'review-label');
    noteLabel.htmlFor = noteId;
    const note = node('textarea');
    note.id = noteId;
    note.value = state.reviewNotes[`${state.run.id}/${source.id}`] ?? source.review?.note ?? '';
    note.addEventListener('input', () => { state.reviewNotes[`${state.run.id}/${source.id}`] = note.value; });
    note.placeholder = 'Reason for keeping or excluding this source';
    note.disabled = !!actionReason();
    const reviewActions = node('div', null, 'card-actions');
    for (const [title, decision] of [['Keep in draft', 'keep'], ['Keep out', 'out']]) {
      const control = button(title, () => mutateSource(source.id, 'review', {decision, note: note.value}));
      control.disabled = !!actionReason();
      control.title = actionReason();
      reviewActions.append(control);
    }
    const review = node('details', null, 'source-review');
    review.open = !readOnly() && state.reviewNotes[`${state.run.id}/${source.id}`] !== undefined;
    review.append(node('summary', readOnly() ? 'Review history' : 'Review source'), noteLabel, note, reviewActions);
    card.append(review);
    if (source.review?.decision) card.append(node('p', `Human review: ${label(source.review.decision)} · ${date(source.review.reviewed_at)}`, 'helper review-info'));
    main.append(card);
    row.append(main);
    for (const [name, title] of [['scope', 'Jev relevance'], ['substance', 'Jev content']]) {
      const answer = source.judgment?.answers?.[name];
      const cell = node('div', null, 'classification-cell');
      cell.append(node('span', title, 'mobile-column-label'),
        node('span', source.judgment?.valid ? choiceLabel(answer?.choice) : 'Not classified'));
      if (source.judgment?.valid && typeof answer?.confidence === 'number') cell.append(node('p', `Confidence ${confidence(answer.confidence)}`, 'helper'));
      row.append(cell);
    }
    container.append(row);
  }
}
function renderBriefing() {
  const run = state.run;
  const briefing = run.briefing || {};
  const container = $('briefing');
  container.replaceChildren();
  for (const [key, title] of [['company', 'Company developments'], ['context', 'Broader context'], ['commentary', 'Commentary']]) {
    const ids = briefing.sections?.[key] || [];
    if (key === 'commentary' && !ids.length) continue;
    const section = node('section', null, 'brief-section');
    section.append(node('h3', title));
    if (!ids.length) section.append(node('p', running() ? 'Awaiting completed research.' : 'No qualifying source found.', 'helper'));
    for (const id of ids) {
      const source = (run.sources || []).find((item) => item.id === id);
      if (!source) { section.append(node('p', `Source ${id} is missing from this capture.`, 'source-error')); continue; }
      const card = node('article', null, 'brief-card');
      card.id = `brief-${id}`;
      card.tabIndex = -1;
      const top = node('div', null, 'source-top');
      top.append(sourceButton(id), node('span', source.domain, 'source-domain'));
      card.append(top, node('h4', source.title || 'Untitled source'), node('p', sourceDates(source), 'source-meta timing'));
      if (key === 'commentary') card.append(node('span', 'Commentary · explicitly reviewed', 'tag'));
      showPassages(card, source);
      showClassifications(card, source);
      card.append(link(source.url, `Open source${source.domain ? ` · ${source.domain}` : ''}`));
      section.append(card);
    }
    container.append(section);
  }
  const unresolved = node('section', null, 'brief-section');
  unresolved.append(node('h3', 'Still unresolved'));
  const items = node('div', null, 'unresolved');
  for (const id of briefing.unresolved || []) items.append(sourceButton(id));
  if (!items.childElementCount) items.append(node('p', running() ? 'Review queue pending.' : 'No unresolved sources listed.', 'helper'));
  unresolved.append(items);
  container.append(unresolved);
}
function renderRun() {
  renderMarket();
  const run = state.run;
  if (!run) return;
   $('run-mode').textContent = readOnly() ? 'Saved · read-only' : 'Research';
  $('run-mode').className = `badge${readOnly() ? ' recorded' : ''}`;
  $('run-title').textContent = `${companyName(run.company)} / ${run.company?.ticker || run.quote?.ticker || ''}`;
  $('run-status').dataset.status = run.status;
  $('run-status').textContent = progress(run);
  $('run-meta').textContent = `Research window: ${date(run.window_start)} to ${date(run.cutoff)}`;
  if (run.kind === 'reclassification') $('run-meta').textContent += ' · Reclassified saved evidence';
  const captured = $('run-quote');
  captured.hidden = false;
  captured.replaceChildren(node('h2', 'Quote used for this run'),
    node('p', `${run.quote?.ticker || run.company?.ticker || 'Ticker not reported'} · ${price(run.quote)} · Daily move ${signed(run.quote?.fields?.day_change)} (${signed(run.quote?.fields?.day_change_percent, '%')})`),
    node('p', `As of ${date(run.quote?.fields?.quote_time)}${priorDate(run.quote, run.created_at) ? ' · Prior-date quote' : ''}`, 'helper'),
    button('Inspect run quote', () => inspectQuote(run.quote)));
  $('reclassify-controls').hidden = !(run.sources || []).length;
  const reclassifyReason = !(state.config?.judge_available ?? state.config?.credentials?.research) ? 'Reclassification requires a Jev API key.'
    : running() || state.busy || state.loading || run.busy ? 'Wait for execution to finish.' : '';
  $('reclassify').disabled = !!reclassifyReason;
  $('reclassify-reason').textContent = reclassifyReason;
  $('reclassify').title = 'Run Jev again on saved sources. Creates a separate result and uses API credits.';
  $('warnings').replaceChildren();
  if (run.settings?.judge_version !== 2) $('warnings').append(node('p', 'Older Jev rubric. Reclassify to apply the updated criteria.', 'warning'));
  for (const warning of run.warnings || []) $('warnings').append(node('p', text(warning), 'warning'));
  for (const format of ['md', 'json']) {
    const anchor = $(`export-${format}`);
    anchor.href = `/api/runs/${encodeURIComponent(run.id)}/export?format=${format}`;
    if (running() || state.busy || run.busy) anchor.setAttribute('aria-disabled', 'true');
    else anchor.removeAttribute('aria-disabled');
    anchor.title = running() || state.busy || run.busy ? 'Wait for execution to finish before exporting.' : '';
    anchor.setAttribute('download', `${run.id}.${format}`);
  }
  renderEvidence();
  renderBriefing();
  if ($('inspector').open) renderInspector();
}
async function loadRuns() {
  const data = await api('/runs');
  const picker = $('saved-runs');
  const current = picker.value;
  picker.replaceChildren(node('option', data.runs.length ? 'Open saved research...' : 'No saved research'));
  picker.firstElementChild.value = '';
  for (const run of data.runs) {
    const option = node('option', `${companyName(run.company)} · ${date(run.created_at)} · ${label(run.status)}`);
    option.value = run.id;
    picker.append(option);
  }
  picker.value = current;
}
function stopPolling() { clearTimeout(state.timer); state.timer = null; }
function pollRun() {
  stopPolling();
  if (!running() || readOnly() || state.loading) return;
  const epoch = state.epoch;
  const id = state.run.id;
  state.timer = setTimeout(async () => {
    try {
      const run = await api(`/runs/${encodeURIComponent(id)}`);
      if (epoch !== state.epoch) return;
      state.run = run;
      renderRun();
      if (!running()) loadRuns().catch((err) => error(`Could not refresh saved runs: ${err.message}`));
    } catch (err) {
      if (epoch !== state.epoch) return;
      error(`Progress update failed: ${err.message} Retrying while this run remains active.`);
    }
    if (epoch === state.epoch) pollRun();
  }, 1000);
}
async function openSaved(id) {
  if (!id) return;
  const epoch = ++state.epoch;
  stopPolling();
  state.loading = true;
  error('');
  renderRun();
  try {
    const run = await api(`/runs/${encodeURIComponent(id)}?replay=true`);
    if (epoch !== state.epoch) return;
    state.run = run;
    state.replay = true;
    state.sourceId = null;
    $('inspector').close();
  } catch (err) { if (epoch === state.epoch) error(`Could not open recorded run: ${err.message}`); }
  finally {
    if (epoch === state.epoch) { state.loading = false; renderRun(); pollRun(); }
  }
}
async function mutateSource(id, action, body) {
  if (actionReason(action === 'expand')) return;
  const epoch = state.epoch;
  const runId = state.run.id;
  state.busy = true;
  error('');
  renderRun();
  try {
    const run = await api(`/runs/${encodeURIComponent(runId)}/sources/${encodeURIComponent(id)}/${action}`, {
      method: 'POST', ...(body ? {body: JSON.stringify(body)} : {})
    });
    if (epoch === state.epoch) state.run = run;
  } catch (err) { error(`${action === 'expand' ? 'Context expansion' : 'Review'} failed: ${err.message}`); }
  finally { state.busy = false; renderRun(); }
}
function inspectSource(id) {
  if (!state.run?.sources?.some((source) => source.id === id)) { error(`Source ${id} is not present in this run.`); return; }
  state.sourceId = id;
  state.inspector = 'source';
  if (!sourceTabs.includes(state.tab)) state.tab = 'Source';
  state.queryId = null;
  document.querySelectorAll('.evidence-card').forEach((card) => card.classList.toggle('selected', card.id === `evidence-${id}`));
  renderInspector();
  if (!$('inspector').open) $('inspector').showModal();
}
function inspectQuote(quote) {
  if (!quote) return;
  state.inspectedQuote = quote; state.inspector = 'quote'; state.tab = 'Quote request';
  renderInspector();
  if (!$('inspector').open) $('inspector').showModal();
}
function renderInspector() {
  const isQuote = state.inspector === 'quote';
  const source = state.run?.sources?.find((item) => item.id === state.sourceId);
  const quote = state.inspectedQuote;
  const tabs = isQuote ? quoteTabs : sourceTabs;
  if ((!isQuote && !source) || (isQuote && !quote)) { $('inspector').close(); return; }
  $('inspector-title').textContent = isQuote ? `Quote / ${quote.ticker}` : `Evidence / ${source.id}`;
  $('inspector-subtitle').textContent = isQuote ? `Retrieved ${date(quote.retrieved_at)} · ${quote.valid ? 'Validated' : 'Invalid'}` : source.title;
  const tablist = $('inspector-tabs');
  tablist.replaceChildren();
  for (const [index, title] of tabs.entries()) {
    const tab = button(title, () => { state.tab = title; renderInspector(); $(`inspection-tab-${index}`).focus(); });
    tab.id = `inspection-tab-${index}`;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(state.tab === title));
    tab.setAttribute('aria-controls', 'inspector-content');
    tab.tabIndex = state.tab === title ? 0 : -1;
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      state.tab = tabs[next]; renderInspector(); $(`inspection-tab-${next}`).focus();
    });
    tablist.append(tab);
  }
  const panel = $('inspector-content');
  panel.setAttribute('aria-labelledby', `inspection-tab-${tabs.indexOf(state.tab)}`);
  panel.replaceChildren();
  if (isQuote) {
    if (state.tab === 'Quote request') {
      panel.append(node('h3', 'Sanitized quote request'));
      json(panel, 'Request', quote.call?.request, true);
      json(panel, 'Call metadata and attempts', quote.call);
    } else {
      panel.append(node('h3', 'Validated fields'));
      facts(panel, {'Quote ID': quote.id, 'Validation': quote.valid ? 'Valid' : 'Invalid', 'Provider': quote.fields?.provider,
        'Quote as of': date(quote.fields?.quote_time), 'Retrieved at': date(quote.retrieved_at), 'Prior-date quote': quote.fields?.prior_date ? 'Yes' : 'No',
        'Currency': quote.fields?.currency, 'Currency source': quote.fields?.currency_source, 'Daily change': signed(quote.fields?.day_change), 'Daily percentage': signed(quote.fields?.day_change_percent, '%')});
      if (quote.error) panel.append(node('p', text(quote.error), 'source-error'));
      json(panel, 'Validated quote fields', quote.fields, true);
      json(panel, 'Actual provider response', quote.call?.response, true);
      json(panel, 'Complete captured quote', quote);
    }
    return;
  }
  if (state.tab === 'Source') {
    panel.append(link(source.url));
    facts(panel, {'Source ID': source.id, 'Publication': source.published_date, 'Date precision': source.publication_precision,
      'Retrieved at': date(source.retrieved_at), 'Timing': label(source.date_check?.timing), 'Query IDs': source.query_ids});
    panel.append(node('p', sourceDates(source), 'source-meta timing'), node('h3', 'Current evidence passages'));
    showPassages(panel, source, true);
    json(panel, 'Original highlights', source.original_highlights, true);
    json(panel, 'Current passages (verbatim)', source.passages);
    json(panel, 'Original URLs and raw references', {original_urls: source.original_urls, raw_refs: source.raw_refs});
    json(panel, 'Context expansion request / response', source.expansion);
    panel.append(node('p', 'Retrieved pages are not historical snapshots. Publication dates do not establish market causation.', 'helper'));
    json(panel, 'Complete source record', source);
    json(panel, 'Run details', {id: state.run.id, created_at: state.run.created_at, window_start: state.run.window_start,
      cutoff: state.run.cutoff, settings: state.run.settings, parent_run_id: state.run.parent_run_id});
  } else if (state.tab === 'Exa request') {
    const queries = (state.run.queries || []).filter((query) => (source.query_ids || []).includes(query.id));
    panel.append(node('h3', 'Originating searches'));
    if (!queries.length) panel.append(node('p', 'No originating query record captured.', 'helper'));
    else {
      if (!queries.some((query) => query.id === state.queryId)) state.queryId = queries[0].id;
      const selectLabel = node('label', 'Query returning this source (including duplicates)');
      selectLabel.htmlFor = 'query-select';
      const select = node('select'); select.id = 'query-select';
      for (const query of queries) { const option = node('option', `${query.id} · ${text(query.request?.query)}`); option.value = query.id; select.append(option); }
      select.value = state.queryId;
      select.addEventListener('change', () => { state.queryId = select.value; renderInspector(); $('query-select').focus(); });
      panel.append(selectLabel, select);
      const query = queries.find((item) => item.id === state.queryId);
      json(panel, 'Exact search request', query.request, true);
      json(panel, 'Actual search response', query.call?.response, true);
      json(panel, 'Search call metadata / attempts', query.call);
    }
    json(panel, 'Expansion call, if requested', source.expansion);
  } else if (state.tab === 'Jev request') {
    panel.append(node('h3', 'Evidence sent to Jev'));
    panel.append(node('p', 'Display previews do not shorten evidence sent to Jev. The stored request shows the exact evidence and two-question rubric used for this source; any context-budget omissions are recorded separately. Jev does not write the briefing.', 'helper'));
    facts(panel, {'Requested model': source.judgment?.requested_model, 'Returned model': source.judgment?.returned_model});
    json(panel, 'Exact request: model, state, and questions', source.judgment?.call?.request, true);
    json(panel, 'Exact rubric used for this source', source.judgment?.call?.request?.body?.questions, true);
    json(panel, 'Omitted passages', source.judgment?.omitted_passages);
  } else if (state.tab === 'Jev response') {
    panel.append(node('h3', 'Jev classification'));
    if (source.previous_judgment) {
      facts(panel, {'Previous content label': choiceLabel(source.previous_judgment.answers?.substance?.choice),
        'New content label': choiceLabel(source.judgment?.answers?.substance?.choice)});
      json(panel, 'Previous classification (unchanged original)', source.previous_judgment);
    }
    showClassifications(panel, source);
    for (const [name, title] of [['scope', 'Relevance (scope)'], ['substance', 'Content (substance)']]) {
      panel.append(node('h3', `${title} distribution`));
      const probabilities = source.judgment?.answers?.[name]?.probabilities;
      if (probabilities && typeof probabilities === 'object') facts(panel, Object.fromEntries(Object.entries(probabilities).map(([choice, value]) => [choiceLabel(choice), confidence(value)])));
      else panel.append(node('p', 'Probability distribution not reported.', 'helper'));
    }
    panel.append(node('p', 'Classification confidence is not factual truth or the probability of causing a price movement.', 'helper'));
    facts(panel, {'Valid response': source.judgment?.valid, 'Error': source.judgment?.error, 'Requested model': source.judgment?.requested_model, 'Returned model': source.judgment?.returned_model});
    json(panel, 'Both classification answers', source.judgment?.answers, true);
    json(panel, 'Actual Jev response', source.judgment?.call?.response, true);
  } else if (state.tab === 'Routing') {
    panel.append(node('h3', 'Routing'));
    panel.append(node('p', 'The app applies rules to the source date and Jev labels. Jev does not decide the final placement; your review can change it.', 'helper'));
    facts(panel, {'Automated destination': destination(source.route?.automated), 'Final destination': destination(source.route?.final), 'Reason': source.route?.reason,
      'Human override': source.review?.decision || 'None', 'Review note': source.review?.note || 'None', 'Reviewed at': date(source.review?.reviewed_at)});
    json(panel, 'Date checks', source.date_check, true);
    json(panel, 'Original Jev labels', source.judgment?.answers, true);
    json(panel, 'Route and human review', {route: source.route, review: source.review});
  } else {
    panel.append(node('h3', 'Usage & timing'));
    json(panel, 'Quote call', state.run.quote?.call);
    for (const query of (state.run.queries || []).filter((item) => (source.query_ids || []).includes(item.id))) json(panel, `Exa search · ${query.id}`, query.call);
    json(panel, 'Jev call metadata', source.judgment?.call, true);
    json(panel, 'Expansion call metadata', source.expansion);
  }
}

$('reclassify').addEventListener('click', async () => {
  if ($('reclassify').disabled || !state.run) return;
  const id = state.run.id;
  const epoch = ++state.epoch;
  stopPolling(); state.busy = true; error(''); renderRun();
  try {
    const run = await api(`/runs/${encodeURIComponent(id)}/reclassify`, {method: 'POST'});
    if (epoch !== state.epoch) return;
    state.run = run; state.replay = false; state.sourceId = null;
    $('saved-runs').value = ''; $('inspector').close();
    loadRuns().catch(err => error(`Could not refresh saved runs: ${err.message}`));
  } catch (err) { if (epoch === state.epoch) error(`Reclassification failed: ${err.message}`); }
  finally { state.busy = false; renderRun(); if (epoch === state.epoch) pollRun(); }
});
$('ticker-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if ($('add-ticker').disabled) return;
  const ticker = $('ticker-input').value.trim().toUpperCase();
  if (ticker.length > 12 || !/^[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*$/.test(ticker)) { error('Enter a valid US ticker, such as MSFT or BRK.B.'); return; }
  state.busy = true; error(''); renderMarket();
  try {
    const {quote, company} = await api('/tickers', {method: 'POST', body: JSON.stringify({ticker})});
    if (!quote?.valid || !company?.ticker || quote.ticker !== company.ticker) throw new Error(quote?.error || 'No valid quote and company returned.');
    state.config.watchlist = [...state.config.watchlist.filter(item => item.ticker !== company.ticker), company];
    state.quotes = [...state.quotes.filter(item => item.ticker !== quote.ticker), quote];
    state.ticker = company.ticker;
    $('ticker-input').value = '';
  } catch (err) { error(`Could not add ticker: ${err.message}`); }
  finally { state.busy = false; renderMarket(); }
});
$('check-prices').addEventListener('click', async () => {
  if (state.busy || state.loading || !state.config?.credentials?.prices) return;
  state.busy = true; error(''); renderMarket();
  try {
    const data = await api('/check-prices', {method: 'POST'});
    state.quotes = data.quotes;
  } catch (err) { error(`Price refresh failed: ${err.message}`); }
  finally { state.busy = false; renderMarket(); }
});
$('research').addEventListener('click', async () => {
  const quote = selectedQuote();
  if ($('research').disabled || !quote?.valid) return;
  const epoch = ++state.epoch;
  stopPolling(); state.busy = true; error(''); renderMarket();
  try {
    const run = await api('/runs', {method: 'POST', body: JSON.stringify({quote_id: quote.id})});
    if (epoch !== state.epoch) return;
    state.run = run; state.replay = false; state.sourceId = null;
    $('saved-runs').value = ''; $('inspector').close();
    loadRuns().catch((err) => error(`Could not refresh saved runs: ${err.message}`));
  } catch (err) { if (epoch === state.epoch) error(`Could not start research: ${err.message}`); }
  finally { state.busy = false; renderRun(); if (epoch === state.epoch) pollRun(); }
});
$('inspect-quote').addEventListener('click', () => inspectQuote(selectedQuote()));
for (const format of ['md', 'json']) {
  $(`export-${format}`).addEventListener('click', async (event) => {
    event.preventDefault();
    const anchor = event.currentTarget;
    if (anchor.getAttribute('aria-disabled') === 'true') return;
    const filename = anchor.getAttribute('download');
    try {
      const response = await fetch(anchor.href);
      if (!response.ok) {
        const data = await response.json();
        throw new Error(text(data.detail || data.error || response.status));
      }
      const url = URL.createObjectURL(await response.blob());
      const download = node('a'); download.href = url; download.download = filename;
      document.body.append(download); download.click(); download.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) { error(`Export failed: ${err.message}`); }
  });
}
$('close-inspector').addEventListener('click', () => $('inspector').close());
$('saved-runs').addEventListener('change', (event) => openSaved(event.target.value));
$('refresh-runs').addEventListener('click', async () => {
  $('refresh-runs').disabled = true;
  try { await loadRuns(); } catch (err) { error(`Could not list saved runs: ${err.message}`); }
  finally { $('refresh-runs').disabled = false; }
});
window.addEventListener('pagehide', stopPolling);
window.addEventListener('pageshow', () => pollRun());

async function initialize() {
  await Promise.all([
    (async () => {
      try {
        state.config = await api('/config'); state.quotes = state.config.quotes || [];
        state.ticker = state.config.watchlist[0]?.ticker || null;
      } catch (err) { error(`Could not load configuration: ${err.message}`); }
      renderMarket();
    })(),
    loadRuns().catch((err) => error(`Could not list recorded runs: ${err.message}`))
  ]);
}
initialize();
