'use strict';
/* ScaleConnect page. Everything shown is put on the page as text, never as HTML,
   so odd characters in a description cannot do anything. */
(function () {
  var app = document.getElementById('app');
  var S = { signedIn: false, csrf: null, caps: { add: true, edit: true, delete: false, notes: {} }, scales: [],
            descMax: 35, pluEdit: false, busy: false, deleteNote: '', configNotice: null };
  var REBOOT = 'The scale did not answer. It may need a reboot. Tell Bart.';
  var routeToken = 0;
  var timer = null;
  var kick = null;
  var formMemory = {};

  var FIELD_LABEL = { price: 'Price', description: 'Description', plu: 'PLU', code: 'Code' };
  var TAG = { ok: 'Done', warn: 'Check this', bad: 'Problem', working: 'Working' };

  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) { return; }
        if (k === 'class') { el.className = v; }
        else if (k === 'text') { el.textContent = v; }
        else if (k.indexOf('on') === 0) { el.addEventListener(k.slice(2), v); }
        else if (k === 'value') { el.value = v; }
        else if (v === true) { el.setAttribute(k, ''); }
        else { el.setAttribute(k, v); }
      });
    }
    for (var i = 2; i < arguments.length; i += 1) { addKids(el, arguments[i]); }
    return el;
  }
  function addKids(el, kid) {
    if (kid === null || kid === undefined || kid === false) { return; }
    if (Array.isArray(kid)) { kid.forEach(function (k) { addKids(el, k); }); return; }
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  function money(price) { return '$' + price; }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }
  function show(nodes) {
    clear(app);
    /* The settings changed after this page started. Nothing can be sent until Bart restarts it. */
    if (S.configNotice) { app.appendChild(box('bad', 'Do not send', S.configNotice)); }
    addKids(app, nodes); window.scrollTo(0, 0);
  }
  function fmtTime(text) {
    if (!text) { return ''; }
    var d = new Date(text);
    return isNaN(d.getTime()) ? String(text) : d.toLocaleString();
  }
  function stopTimer() { if (timer) { clearTimeout(timer); timer = null; } kick = null; }
  function link(href, text, cls) { return h('a', { class: cls || 'btn', href: href, text: text }); }
  function box(cls, tag, line, more) {
    return h('div', { class: 'box ' + cls }, tag ? h('span', { class: 'tag', text: tag }) : null,
             h('div', { class: 'line', text: line }), more ? h('div', { class: 'more', text: more }) : null);
  }

  /* talking to the server */

  function api(method, path, body) {
    var opts = { method: method, credentials: 'same-origin', headers: {} };
    if (method !== 'GET') {
      opts.headers['Content-Type'] = 'application/json';
      if (S.csrf) { opts.headers['X-CSRF-Token'] = S.csrf; }
      opts.body = JSON.stringify(body || {});
    }
    return fetch(path, opts).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var err = (data && data.error) || { code: 'http_' + res.status, message: 'Something went wrong (code ' + res.status + ').' };
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }, function () {
      throw { code: 'network', message: 'Could not reach the server. Check the WiFi and try again.' };
    });
  }

  function loadState() {
    return api('GET', '/api/state').then(function (d) {
      S.signedIn = d.signed_in; S.csrf = d.csrf; S.caps = d.capabilities; S.scales = d.scales;
      S.descMax = d.description_max_length; S.pluEdit = d.plu_edit_enabled; S.busy = d.busy;
      S.deleteNote = d.delete_note; S.configNotice = d.config_notice || null;
    });
  }

  function askPin() {
    return new Promise(function (resolve, reject) {
      var input = h('input', { type: 'password', id: 'pin', autocomplete: 'off', 'aria-label': 'PIN' });
      var msg = h('div', { id: 'pinmsg' });
      var go = h('button', { class: 'btn', type: 'submit', text: 'Unlock' });
      var cover = h('div', { class: 'pin-cover', role: 'dialog', 'aria-modal': 'true' },
        h('form', { class: 'pin-card' },
          h('h1', { text: 'Enter the PIN' }),
          h('p', { class: 'plain', text: 'The PIN is needed to make changes.' }),
          input, msg, go,
          h('button', { class: 'btn second', type: 'button', text: 'Cancel', onclick: function () {
            document.body.removeChild(cover); reject({ code: 'cancelled', message: 'Cancelled.' }); } })));
      cover.querySelector('form').addEventListener('submit', function (ev) {
        ev.preventDefault();
        go.disabled = true;
        clear(msg);
        api('POST', '/api/login', { pin: input.value }).then(function (d) {
          S.signedIn = true; S.csrf = d.csrf;
          document.body.removeChild(cover);
          resolve();
        }, function (e) {
          go.disabled = false; input.value = '';
          msg.appendChild(box('bad', 'Problem', e.message));
          input.focus();
        });
      });
      document.body.appendChild(cover);
      input.focus();
    });
  }

  function withPin(fn) {
    return fn().catch(function (e) {
      if (e && (e.code === 'pin_required' || e.code === 'bad_csrf')) {
        var again = e.code === 'bad_csrf' ? loadState() : Promise.resolve();
        return again.then(function () { return S.signedIn ? null : askPin(); }).then(function () { return fn(); });
      }
      throw e;
    });
  }

  function errorBox(e) { return box('bad', 'Problem', (e && e.message) || 'Something went wrong.'); }

  /* drafts */

  function saveDraft(draft) { try { sessionStorage.setItem('sc_draft', JSON.stringify(draft)); } catch (e) { /* ignore */ } }
  function loadDraft() { try { return JSON.parse(sessionStorage.getItem('sc_draft')); } catch (e) { return null; } }
  function dropDraft() { try { sessionStorage.removeItem('sc_draft'); } catch (e) { /* ignore */ } }
  function saveLast(pid, title) {
    try { localStorage.setItem('sc_last', JSON.stringify({ pid: pid, title: title, at: new Date().toISOString() })); } catch (e) { /* ignore */ }
  }
  function loadLast() { try { return JSON.parse(localStorage.getItem('sc_last')); } catch (e) { return null; } }

  /* pieces used by several screens */

  function fieldLine(name, before, after) {
    var label = FIELD_LABEL[name] || name;
    var fmt = function (v) { return name === 'price' && v !== null && v !== undefined ? money(v) : String(v === null || v === undefined ? '' : v); };
    return h('div', { class: 'field-row' }, h('strong', { text: label + ': ' }),
      before !== null && before !== undefined && before !== '' ? [h('span', { class: 'old', text: fmt(before) }), ' to '] : null,
      h('span', { class: 'new', text: fmt(after) }));
  }

  /* A change of PLU: returns { before, after } or null */
  function pluChange(d) {
    var f = (d.fields || {}).plu;
    return d.action === 'edit' && f && f.before && f.after && f.before !== f.after ? f : null;
  }
  /* Which library warning belongs to this difference, for the two changes that stop a PLU working */
  function stopKind(d) { return d.action === 'delete' ? 'delete_placeholder' : (pluChange(d) ? 'plu_change' : null); }
  function stopText(d) {
    if (d.action === 'delete') {
      var gone = d.before || {};
      return 'PLU ' + (gone.plu || d.plu) + ' will stop working on the scales once this runs.';
    }
    var pc = pluChange(d);
    return 'The old PLU ' + pc.before + ' will stop working on the scales once this runs. PLU ' + pc.after + ' takes its place.';
  }
  function ackText(d) {
    if (d.action === 'delete') { return 'I understand. PLU ' + ((d.before || {}).plu || d.plu) + ' will stop working. Delete it.'; }
    var pc = pluChange(d);
    return 'I understand. PLU ' + pc.before + ' will stop working. Change it to ' + pc.after + '.';
  }

  /* A repeat finishes work an earlier change left on some scale. The library shows no field change for a
     PLU change that is only being finished, so these say what it does. */
  function oldText(rep) { return rep.old && rep.old.length ? 'PLU ' + rep.old.join(' and PLU ') : 'the old PLU'; }
  function repeatBox(rep) {
    if (rep.kind === 'delete') {
      return box('warn', 'Repeat', 'Finish the delete of PLU ' + rep.plu + '.',
        'This sends the same delete again. PLU ' + rep.plu + ' stops working on any scale that still has it.');
    }
    return box('warn', 'Repeat', 'Finish the PLU change to ' + rep.plu + '.',
      'This sends only the PLU. Price and name are not changed. Every scale you send to gets PLU ' + rep.plu + ' on this item, and ' +
      oldText(rep) + ' stops working on it. A scale that already has PLU ' + rep.plu + ' gets the same record again, with a new edit time.');
  }
  function repeatTick(rep) {
    var old = oldText(rep);
    return 'I understand. ' + old.charAt(0).toUpperCase() + old.slice(1) + ' will stop working on a scale that still has it.';
  }

  function diffBlock(d, sent) {    var title; var cls = 'box'; var lines = [];
    var item = d.after || d.before || {};
    if (d.action === 'add') {
      title = 'Add a new item';
      lines.push(fieldLine('plu', null, d.plu || item.plu));
      lines.push(fieldLine('description', null, item.description));
      lines.push(fieldLine('price', null, item.price));
    } else if (d.action === 'settle_accept' || d.action === 'settle_push') {
      title = d.action === 'settle_accept' ? 'Our list will copy what the scale holds for item ' : 'The scale will get our list\'s values for item ';
      title += (d.plu || item.plu || '') + ' ' + ((d.before || {}).description || '');
      Object.keys(d.fields || {}).forEach(function (k) {
        if (k === 'timestamp') { return; }
        lines.push(fieldLine(k, d.fields[k].before, d.fields[k].after));
      });
    } else if (d.action === 'delete') {
      var gone = d.before || item;
      title = sent ? 'Delete this item' : 'You are about to delete this item'; cls = 'box bad';
      lines.push(h('div', { class: 'big', text: gone.plu || d.plu || '' }));
      lines.push(h('div', { class: 'line', text: gone.description || '' }));
      if (gone.price) { lines.push(h('div', { class: 'big', text: money(gone.price) })); }
      if (!sent) { lines.push(h('p', { class: 'plain stop', text: stopText(d) + ' The slot will show UNUSED.' })); }
    } else {
      var pc = pluChange(d);
      var was = d.before || {};
      title = 'Change item ' + (was.plu || d.plu || item.plu || '') + ' ' + (was.description || '');
      var keys = Object.keys(d.fields || {}).filter(function (k) { return k !== 'timestamp'; });
      /* PLU first. Code always follows the PLU, so it is not shown as a second change. */
      keys.sort(function (a, b) { return (a === 'plu' ? 0 : 1) - (b === 'plu' ? 0 : 1); });
      keys.forEach(function (k) {
        if (k === 'code' && pc && d.fields.code.after === pc.after) { return; }
        lines.push(fieldLine(k, d.fields[k].before, d.fields[k].after));
      });
      if (pc && !sent) {
        lines.push(h('p', { class: 'plain stop', text: stopText(d) }));
      }
    }
    return h('div', { class: cls }, h('div', { class: 'line', text: title }), lines);
  }

  function checkbox(id, text, onchange) {
    return h('label', { class: 'check', for: id },
      h('input', { type: 'checkbox', id: id, onchange: onchange }), h('span', { text: text }));
  }

  /* Start the review of a repeat. The person still reviews it and ticks before anything is sent.
     meta says what is being finished, so the review can say so plainly. */
  function startRepeat(title, change, meta, back) {
    saveDraft({ title: title, changes: [change], back: back || '#/', backText: 'Go back', repeat: meta });
    /* From a refused settle we are already on the review address, so the hash would not change. */
    if (location.hash === '#/review') { route(); } else { location.hash = '#/review'; }
  }

  /* Work that some scale has not confirmed. From our list, with no scale contact. A delete or a PLU change
     is repeated. A change that missed one scale is settled on that scale. A card with no button asks for a person instead. */
  function unfinishedBoxes(items) {
    return (items || []).map(function (c) {
      if (c.kind === 'missed_change') {
        return h('div', null,
          box('bad', 'Not finished', c.text, c.more),
          c.button ? link('#/settle/' + encodeURIComponent(c.plu) + '/' + encodeURIComponent(c.scales[0]), c.button, 'btn second') : null);
      }
      if (!c.button) { return h('div', null, box('bad', 'Not finished', c.text, c.more)); }
      return h('div', null,
        box('bad', 'Not finished', c.text, c.more),
        h('button', { class: 'btn second', type: 'button', text: c.button, onclick: function () {
          startRepeat('Repeat ' + c.kind + ' ' + c.target, c.repeat, { kind: c.kind, plu: c.target, old: c.stopping || [] }, '#/');
        } }));
    });
  }

  /* home: search */

  function renderHome(my) {
    var results = h('div', { id: 'results' });
    var hint = h('p', { class: 'plain', text: 'Type a PLU number, or part of the name.' });
    var input = h('input', { type: 'search', id: 'q', placeholder: 'PLU or name', autocomplete: 'off',
                             inputmode: 'search', 'aria-label': 'Search by PLU or description' });
    var seq = 0; var wait = null;
    function run() {
      var q = input.value.trim(); var mine = ++seq;
      if (!q) { clear(results); results.appendChild(hint); return; }
      api('GET', '/api/search?q=' + encodeURIComponent(q)).then(function (d) {
        if (mine !== seq || my !== routeToken) { return; }
        clear(results);
        if (!d.items.length) { results.appendChild(box('warn', null, 'Nothing found for that.', 'Try fewer letters, or check the PLU.')); }
        d.items.forEach(function (i) {
          results.appendChild(h('a', { class: 'result', href: '#/item/' + encodeURIComponent(i.plu) },
            h('span', { class: 'price', text: money(i.price) }),
            h('span', { class: 'plu', text: i.plu }),
            h('div', { class: 'desc', text: i.description })));
        });
      }, function (e) { if (mine === seq) { clear(results); results.appendChild(errorBox(e)); } });
    }
    input.addEventListener('input', function () { clearTimeout(wait); wait = setTimeout(run, 150); });
    input.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { clearTimeout(wait); run(); } });
    var off = S.scales.filter(function (s) { return !s.write_enabled; }).map(function (s) { return s.name; });
    var last = loadLast();
    var unf = h('div', { id: 'unfinished' });
    var nodes = [h('h1', { text: 'Find an item' }), unf, input, results];
    if (S.busy) { nodes.unshift(box('working', 'Working', 'A change is being sent right now.', 'You can still look up items.')); }
    if (last && last.pid) {
      nodes.push(h('a', { class: 'btn second', href: '#/result/' + encodeURIComponent(last.pid), text: 'See the last change I sent' }));
    }
    nodes.push(link('#/add', 'Add a new item', 'btn second'));
    nodes.push(h('p', { class: 'small' }, h('a', { href: '#/orphans', text: 'Bart only: mark a scale number as taken' })));
    if (off.length) { nodes.push(h('p', { class: 'small', text: 'Changes are turned off for: ' + off.join(', ') + '.' })); }
    show(nodes);
    results.appendChild(hint);
    input.focus();
    /* Unfinished deletes and PLU changes. A failure here must not spoil the search above. */
    api('GET', '/api/unfinished').then(function (d) {
      if (my !== routeToken || !d.items.length) { return; }
      unf.appendChild(h('h2', { text: 'Not finished' }));
      unfinishedBoxes(d.items).forEach(function (n) { unf.appendChild(n); });
    }, function () { /* the list is a help, not a need */ });
  }

  /* one item */

  /* What a read now found wrong, in plain words. A scale that gave no reply may be frozen. */
  function readProblems(checked) {
    var wrong = (checked || []).filter(function (s) { return s.status !== 'read'; });
    if (!wrong.length) { return null; }
    return wrong.map(function (s) {
      return box(s.severity === 'warn' ? 'warn' : 'bad', 'Check this', s.may_be_frozen ? REBOOT : s.text, s.may_be_frozen ? s.text : null);
    });
  }

  function renderItem(plu, my, checked) {
    return api('GET', '/api/item/' + encodeURIComponent(plu)).then(function (d) {
      if (my !== routeToken) { return; }
      var it = d.item;
      var rows = d.scales.map(function (s) {
        var bad = s.flag === 'different' || s.flag === 'different_product' || s.flag === 'missing' || s.flag === 'unfinished';
        var cls = s.flag === 'same' ? 'ok' : (bad ? 'bad' : (s.flag === 'unsure' ? 'warn' : ''));
        var tag = s.flag === 'same' ? 'Matches' : (s.flag === 'unfinished' ? 'Not finished' : (s.flag === 'different_product' ? 'Different product' : (bad ? 'Different' : (s.flag === 'unsure' ? 'Not sure' : 'Unknown'))));
        var more = s.last_read ? 'Last read: PLU ' + s.last_read.plu + ', ' + s.last_read.description + ', ' + money(s.last_read.price) +
          (s.last_read_at ? ' (' + fmtTime(s.last_read_at) + ')' : '') : null;
        var extra = s.can_change ? null : 'Changes are turned off for this scale.';
        var box1 = h('div', { class: 'box ' + cls }, h('span', { class: 'tag', text: tag }),
          h('div', { class: 'line', text: s.scale }), h('div', { class: 'more', text: s.text }),
          more ? h('div', { class: 'more', text: more }) : null, extra ? h('div', { class: 'more', text: extra }) : null);
        if (s.flag === 'unfinished' && s.repeat) {
          /* Settling would undo the PLU change on the other scales, so only the repeat is offered. */
          box1.appendChild(h('button', { class: 'btn second', type: 'button', text: 'Repeat the PLU change', onclick: function () {
            var meta = s.repeat_meta || { plu: it.plu, old: [] };
            startRepeat('Repeat the PLU change to ' + meta.plu, s.repeat, { kind: 'plu_change', plu: meta.plu, old: meta.old || [] }, '#/item/' + encodeURIComponent(it.plu));
          } }));
        } else if (s.can_fix) {
          box1.appendChild(link('#/settle/' + encodeURIComponent(it.plu) + '/' + encodeURIComponent(s.scale), (s.flag === 'different_product' ? 'Bart only: settle this on ' : 'Fix this difference on ') + s.scale, 'btn second'));
        }
        return box1;
      });
      /* A check reads every scale, and one that did not answer must not be read again until someone has looked at it. */
      var stuck = d.scales.some(function (s) { return s.missed && s.missed.kind === 'frozen'; });
      var nodes = [
        h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })),
        h('div', { class: 'box' }, h('div', { class: 'big', text: it.plu }), h('div', { class: 'line', text: it.description }),
          h('div', { class: 'big', text: money(it.price) })),
        S.caps.edit ? link('#/edit/' + encodeURIComponent(it.plu), 'Change this item') : null,
        readProblems(checked),
        h('h2', { text: 'On the scales' }), rows,
        h('div', { id: 'checkmsg' }),
        stuck ? h('p', { class: 'plain', text: 'A check reads every scale, and one did not answer. Tell Bart.' }) :
        h('button', { class: 'btn second', type: 'button', text: 'Check the scales now', onclick: function () {
          var m = document.getElementById('checkmsg'); clear(m);
          m.appendChild(box('working', 'Working', 'Reading the scales. This can take a minute.'));
          withPin(function () { return api('POST', '/api/check', { plu: it.plu }); }).then(function (r) { renderItem(plu, my, r.scales); },
            function (e) { clear(m); m.appendChild(errorBox(e)); });
        } })
      ];
      if (d.sending_now) {
        nodes.push(box('working', 'Working', 'A change is being sent right now.', 'Wait for it to finish before changing this item.'));
      } else if (d.interrupted && d.interrupted.length) {
        d.interrupted.forEach(function (c) {
          nodes.push(box('warn', 'Check this', 'An earlier change to this item never finished.', 'Look at the scales. Then open the change and close it.'));
          nodes.push(link('#/result/' + encodeURIComponent(c.preview_id), 'Open that change', 'btn second'));
        });
      }
      if (S.caps['delete']) {
        nodes.push(h('h2', { text: 'Delete' }));
        nodes.push(h('p', { class: 'small', text: 'You will see this item again to check it before anything is sent.' }));
        nodes.push(h('button', { class: 'btn danger', type: 'button', text: 'Delete this item', onclick: function () {
          saveDraft({ title: 'Delete ' + it.plu, changes: [{ action: 'delete', target_plu: it.plu }],
                      back: '#/item/' + encodeURIComponent(it.plu), backText: 'Do not delete. Go back.' });
          location.hash = '#/review';
        } }));
      } else if (S.deleteNote) {
        nodes.push(h('p', { class: 'small', text: S.deleteNote }));
      }
      show(nodes);
    }, function (e) {
      if (my === routeToken) { show([h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })), errorBox(e)]); }
    });
  }

  /* edit */

  function renderEdit(plu, my) {
    return api('GET', '/api/item/' + encodeURIComponent(plu)).then(function (d) {
      if (my !== routeToken) { return; }
      var it = d.item;
      var mem = formMemory['edit/' + it.plu] || {};
      var price = h('input', { type: 'text', id: 'price', inputmode: 'decimal', autocomplete: 'off', value: mem.price !== undefined ? mem.price : it.price });
      var desc = h('input', { type: 'text', id: 'desc', autocomplete: 'off', maxlength: String(S.descMax), value: mem.desc !== undefined ? mem.desc : it.description });
      var pluInput = h('input', { type: 'text', id: 'plu', inputmode: 'numeric', maxlength: '4', autocomplete: 'off', value: mem.plu !== undefined ? mem.plu : it.plu, readonly: S.pluEdit ? null : true });
      var msg = h('div');
      var nodes = [
        h('p', { class: 'small' }, h('a', { href: '#/item/' + encodeURIComponent(it.plu), text: 'Back to the item' })),
        h('h1', { text: 'Change item' }),
        h('label', { for: 'price' }, 'Price', h('span', { class: 'hint', text: 'For example 9.99' })), price,
        h('label', { for: 'desc' }, 'Description', h('span', { class: 'hint', text: 'Up to ' + S.descMax + ' letters or numbers' })), desc,
        h('label', { for: 'plu' }, 'PLU', S.pluEdit ? h('span', { class: 'hint', text: '4 digits. If you change it, the old PLU stops working on the scales.' }) : null), pluInput,
        S.pluEdit ? null : h('p', { class: 'small', text: 'The PLU cannot be changed here. Ask Bart.' }),
        msg,
        h('div', { class: 'sticky' }, h('button', { class: 'btn', type: 'button', text: 'Review the change', onclick: function () {
          clear(msg);
          formMemory['edit/' + it.plu] = { price: price.value, desc: desc.value, plu: pluInput.value };
          var change = { action: 'edit', target_plu: it.plu };
          var any = false;
          if (price.value.trim() !== it.price) { change.price = price.value; any = true; }
          if (desc.value.trim() !== it.description) { change.description = desc.value; any = true; }
          if (S.pluEdit && pluInput.value.trim() !== it.plu) { change.plu = pluInput.value; any = true; }
          if (!any) { msg.appendChild(box('warn', null, 'Nothing is different yet.', S.pluEdit ? 'Change the price, the description or the PLU first.' : 'Change the price or the description first.')); return; }
          if (change.plu !== undefined && !/^\d{4}$/.test(change.plu.trim())) {
            msg.appendChild(box('warn', null, 'The PLU must be 4 digits.')); return;
          }
          saveDraft({ title: 'Change ' + it.plu, changes: [change], back: '#/edit/' + encodeURIComponent(it.plu) });
          location.hash = '#/review';
        } }))
      ];
      show(nodes);
      price.focus(); price.select();
    }, function (e) {
      if (my === routeToken) { show([h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })), errorBox(e)]); }
    });
  }

  /* add */

  function renderAdd() {
    var mem = formMemory.add || {};
    var plu = h('input', { type: 'text', id: 'plu', inputmode: 'numeric', maxlength: '4', autocomplete: 'off', value: mem.plu || '' });
    var desc = h('input', { type: 'text', id: 'desc', autocomplete: 'off', maxlength: String(S.descMax), value: mem.desc || '' });
    var price = h('input', { type: 'text', id: 'price', inputmode: 'decimal', autocomplete: 'off', value: mem.price || '' });
    var w = h('input', { type: 'radio', name: 'soldby', value: 'weight', id: 'by-weight' });
    var p = h('input', { type: 'radio', name: 'soldby', value: 'piece', id: 'by-piece' });
    if (mem.soldBy === 'piece') { p.checked = true; } else { w.checked = true; }
    var msg = h('div');
    var orphan = h('input', { type: 'text', id: 'orphan', inputmode: 'numeric', maxlength: '3', autocomplete: 'off', value: mem.orphan || '' });
    show([
      h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })),
      h('h1', { text: 'Add a new item' }),
      h('label', { for: 'plu' }, 'PLU', h('span', { class: 'hint', text: '4 digits' })), plu,
      h('label', { for: 'desc' }, 'Description', h('span', { class: 'hint', text: 'Up to ' + S.descMax + ' letters or numbers' })), desc,
      h('label', { for: 'price' }, 'Price', h('span', { class: 'hint', text: 'For example 9.99' })), price,
      h('label', { text: 'Sold by' }),
      h('div', { class: 'seg' }, h('label', { for: 'by-weight' }, w, h('span', { text: 'By weight' })),
        h('label', { for: 'by-piece' }, p, h('span', { text: 'By piece' }))),
      h('details', null, h('summary', { text: 'Bart only: use a marked scale number' }),
        h('label', { for: 'orphan' }, 'Scale number to replace', h('span', { class: 'hint', text: 'Only a number marked as taken. Leave empty for a normal add.' })), orphan),
      msg,
      h('div', { class: 'sticky' }, h('button', { class: 'btn', type: 'button', text: 'Review the new item', onclick: function () {
        clear(msg);
        var soldBy = p.checked ? 'piece' : 'weight';
        formMemory.add = { plu: plu.value, desc: desc.value, price: price.value, soldBy: soldBy, orphan: orphan.value };
        if (!plu.value.trim() || !desc.value.trim() || !price.value.trim()) {
          msg.appendChild(box('warn', null, 'Fill in the PLU, the description and the price.')); return;
        }
        var add = { action: 'add', plu: plu.value, description: desc.value, price: price.value, sold_by: soldBy };
        if (orphan.value.trim()) { add.replace_orphan_id = parseInt(orphan.value, 10); }
        saveDraft({ title: 'Add ' + plu.value.trim(), changes: [add], back: '#/add' });
        location.hash = '#/review';
      } }))
    ]);
    plu.focus();
  }

  /* close a change that stopped */

  function resolveForm(changeId, after) {
    var note = h('input', { type: 'text', id: 'note', autocomplete: 'off', maxlength: '150', placeholder: 'What did you see on the scales?' });
    var msg = h('div');
    return h('div', { class: 'box' },
      h('div', { class: 'line', text: 'Bart only' }),
      h('p', { class: 'plain', text: 'Look at every scale first. Then write what you saw, and close this change.' }),
      note, msg,
      h('button', { class: 'btn second', type: 'button', text: 'I looked. Close this change.', onclick: function () {
        clear(msg);
        if (note.value.trim().length < 3) { msg.appendChild(box('warn', null, 'Write what you saw first.')); return; }
        withPin(function () { return api('POST', '/api/resolve', { change_id: changeId, note: note.value }); }).then(after, function (e) {
          msg.appendChild(errorBox(e));
        });
      } }));
  }

  /* A change that missed this scale. Nobody may know what it holds, so nobody is asked to pick a side.
     The only choice is our list, or calling Bart. A scale that did not answer, or a change our list did not take, has no choice at all. */
  function renderMissedSettle(it, scale, miss, go) {
    var top = [
      h('p', { class: 'small' }, h('a', { href: '#/item/' + encodeURIComponent(it.plu), text: 'Back to the item' })),
      h('h1', { text: 'Finish a change on ' + scale }),
      h('div', { class: 'box' }, h('div', { class: 'big', text: it.plu }), h('div', { class: 'line', text: it.description }),
        h('div', { class: 'big', text: money(it.price) })),
      h('p', { class: 'plain', text: miss.status_text })
    ];
    if (miss.kind !== 'fixable') { return top.concat([box('bad', 'Check this', miss.stop_text)]); }
    return top.concat([
      h('p', { class: 'plain', text: miss.text }),
      h('button', { class: 'btn', type: 'button', text: 'Our list is right. Put it on ' + scale + '.', onclick: function () { go('catalog'); } }),
      h('p', { class: 'small', text: 'The scale will be changed, then read back to check it.' }),
      h('p', { class: 'plain', text: 'If you are not sure, call Bart.' })
    ]);
  }

  /* settle a difference on one scale. Bart picks which side wins. */

  function renderSettle(plu, scale, my) {
    return api('GET', '/api/item/' + encodeURIComponent(plu)).then(function (d) {
      if (my !== routeToken) { return; }
      var it = d.item;
      function go(take) {
        saveDraft({ title: 'Settle ' + it.plu + ' on ' + scale, changes: [], settle: { scale: scale, take: take, plu: it.plu },
                    back: '#/item/' + encodeURIComponent(it.plu) });
        location.hash = '#/review';
      }
      var mine = d.scales.filter(function (s) { return s.scale === scale; })[0];
      if (mine && mine.missed) { show(renderMissedSettle(it, scale, mine.missed, go)); return; }
      show([
        h('p', { class: 'small' }, h('a', { href: '#/item/' + encodeURIComponent(it.plu), text: 'Back to the item' })),
        h('h1', { text: 'Fix a difference' }),
        h('div', { class: 'box' }, h('div', { class: 'big', text: it.plu }), h('div', { class: 'line', text: it.description }),
          h('div', { class: 'big', text: money(it.price) })),
        h('p', { class: 'plain', text: scale + ' holds something different from our list. Pick which one is right.' }),
        h('button', { class: 'btn', type: 'button', text: 'The scale is right. Copy it into our list.', onclick: function () { go('scale'); } }),
        h('p', { class: 'small', text: 'Nothing is sent to the scale. Other scales may then differ from our list.' }),
        h('button', { class: 'btn', type: 'button', text: 'Our list is right. Put it on ' + scale + '.', onclick: function () { go('catalog'); } }),
        h('p', { class: 'small', text: 'The scale will be changed, then read back to check it.' })
      ]);
    }, function (e) {
      if (my === routeToken) { show([h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })), errorBox(e)]); }
    });
  }

  /* mark a scale number as holding something we do not list */

  function renderOrphans() {
    var scale = h('select', { id: 'scale' }, S.scales.map(function (s) { return h('option', { value: s.name, text: s.name }); }));
    var num = h('input', { type: 'text', id: 'num', inputmode: 'numeric', maxlength: '3', autocomplete: 'off' });
    var note = h('input', { type: 'text', id: 'onote', autocomplete: 'off', maxlength: '100' });
    var msg = h('div');
    show([
      h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })),
      h('h1', { text: 'Mark a scale number as taken' }),
      h('p', { class: 'plain', text: 'Use this only when a scale holds a product at a number that our list does not know. For example an old test item. Ask Bart first. After this, a new item can be told to replace it.' }),
      h('label', { for: 'scale', text: 'Scale' }), scale,
      h('label', { for: 'num' }, 'Scale number', h('span', { class: 'hint', text: 'A number from 1 to 500' })), num,
      h('label', { for: 'onote' }, 'Note', h('span', { class: 'hint', text: 'What is it? For example, old test item' })), note,
      msg,
      h('div', { class: 'sticky' }, h('button', { class: 'btn', type: 'button', text: 'Mark it as taken', onclick: function () {
        clear(msg);
        var n = parseInt(num.value, 10);
        if (!(n >= 1 && n <= 500)) { msg.appendChild(box('warn', null, 'Type a number from 1 to 500.')); return; }
        withPin(function () { return api('POST', '/api/orphan', { scale: scale.value, id: n, note: note.value }); }).then(function (r) {
          msg.appendChild(box('ok', 'Done', r.message));
        }, function (e) { msg.appendChild(errorBox(e)); });
      } }))
    ]);
  }
  /* review */

  function renderReview(my) {
    var draft = loadDraft();
    if (!draft || !draft.changes) {
      show([box('warn', null, 'There is nothing to review.'), link('#/', 'Back to search')]); return;
    }
    var acked = {};           /* keys of ticked boxes, kept when the page is redrawn */
    var holder = h('div');
    var latest = null; var checking = true; var checkError = null;
    show([h('h1', { text: 'Check before sending' }), holder]);

    function paint() {
      if (my !== routeToken) { return; }
      clear(holder);
      var pv = latest;
      var back = link(draft.back || '#/', draft.backText || 'Go back and fix', 'btn second');
      if (!pv) { holder.appendChild(h('p', { class: 'plain dots', text: 'Getting ready' })); return; }
      if (!pv.ok) {
        holder.appendChild(box('bad', 'Problem', 'This cannot be sent yet.'));
        pv.errors.forEach(function (e) { holder.appendChild(box('bad', null, e.may_be_frozen ? REBOOT : e.message, e.may_be_frozen ? e.message : null)); });
        if (draft.repeat) { holder.appendChild(box('warn', null, 'This repeat is out of date.', 'The work may already be finished, or the item may have changed. Nothing was sent.')); }
        if (pv.repeat) {
          /* Settling cannot finish a PLU change. The repeat can. */
          holder.appendChild(h('button', { class: 'btn', type: 'button', text: 'Repeat the PLU change', onclick: function () {
            startRepeat('Repeat the PLU change to ' + pv.repeat.plu, pv.repeat.change, { kind: 'plu_change', plu: pv.repeat.plu, old: pv.repeat.old || [] }, draft.back);
          } }));
        }
        holder.appendChild(back); return;
      }
      var rep = draft.repeat || null;
      var blocked = false; var needed = [];
      /* A repeat is about one item, named by its PLU. If the review shows another item, nothing may be sent. */
      var wrongItem = !!rep && (pv.differences.length === 0 || pv.differences.some(function (d) {
        var shown = rep.kind === 'delete' ? (d.action === 'delete' ? (d.before || {}).plu : null) : (d.action === 'edit' ? (d.after || {}).plu : null);
        return shown !== rep.plu;
      }));
      if (rep && !wrongItem) { holder.appendChild(repeatBox(rep)); }
      if (wrongItem) {
        blocked = true;
        holder.appendChild(box('bad', 'Do not send', 'This is not the item this repeat was about.', 'Go back. Nothing was sent.'));
      }
      pv.differences.forEach(function (d) { holder.appendChild(diffBlock(d)); });

      function tick(id, key, text) {
        needed.push(key);
        holder.appendChild(checkbox(id, text, function (ev) { acked[key] = ev.target.checked; update(); }));
        if (acked[key]) { holder.lastChild.querySelector('input').checked = true; }
      }
      /* A delete or a PLU change stops a PLU working. The block above already says which item.
         Each one needs its own tick, even if the library did not ask for one. */
      var stopTicked = {}; var repTicked = false; var frozenSeen = false;
      pv.warnings.forEach(function (wn, i) {
        var key = 'w:' + wn.code + ':' + i;
        var diff = pv.differences.filter(function (x) { return x.index === wn.index; })[0];
        if (rep && wn.code === 'no_change') { return; }   /* the repeat above explains it */
        if (diff && wn.code === stopKind(diff)) {
          if (wn.requires_ack) { stopTicked[diff.index] = true; tick('ack' + i, key, ackText(diff)); }
          return;
        }
        if (rep && rep.kind === 'plu_change' && wn.code === 'plu_change') {
          /* Finishing a PLU change shows no field change, so the repeat box above says what it does. */
          if (wn.requires_ack) { repTicked = true; tick('ack' + i, key, repeatTick(rep)); }
          return;
        }
        if (wn.code === 'gap_filled') {
          holder.appendChild(box('warn', 'Read this', wn.plain || wn.message));
          if (wn.requires_ack) { tick('ack' + i, key, 'I understand. Empty slots will be added so there is no gap.'); }
          return;
        }
        if (wn.may_be_frozen) { frozenSeen = true; holder.appendChild(box('bad', 'Check this', REBOOT, wn.message)); }
        else { holder.appendChild(box(wn.requires_ack ? 'warn' : 'working', wn.requires_ack ? 'Read this' : 'Note', wn.message)); }
        if (wn.requires_ack) { tick('ack' + i, key, 'I have read this. Send it anyway.'); }
      });
      pv.differences.forEach(function (d, i) {
        if (stopKind(d) && !stopTicked[d.index]) { tick('stop' + i, 'p:' + d.index, ackText(d)); }
      });
      /* A repeat of a PLU change always needs its tick. Normally the library asks. If it did not, the page does. */
      /* A PLU change that shows its own PLU line has its own tick above, so the repeat does not ask twice. */
      var ownLine = pv.differences.some(function (d) { return !!pluChange(d); });
      if (rep && !wrongItem && rep.kind === 'plu_change' && !repTicked && !ownLine) { tick('repeat', 'r:repeat', repeatTick(rep)); }
      if (checking) {
        holder.appendChild(box('working', 'Working', 'Checking the scales takes about a minute and a half.', 'Please wait. Do not refresh the page, and do not tap anything twice.'));
      }
      if (S.configNotice) { holder.appendChild(box('bad', 'Do not send', S.configNotice)); }
      holder.appendChild(h('h2', { text: 'The scales' }));
      pv.scales.forEach(function (name) {
        var read = pv.scales_read.indexOf(name) >= 0;
        var mine = pv.drift.filter(function (d) { return d.scale === name; });
        var line; var cls;
        var named = pv.warnings.filter(function (w) { return (w.scales_named || []).indexOf(name) >= 0; });
        if (checking) { line = 'Checking'; cls = 'working'; }
        else if (mine.length) { line = 'Different from our list'; cls = 'bad'; }
        else if (named.some(function (w) { return w.may_be_frozen; })) { line = 'Did not answer. Do not send. Tell Bart.'; cls = 'bad'; }
        else if (named.length) { line = 'Read the note above.'; cls = 'warn'; }
        else if (read && rep && rep.kind === 'plu_change') { line = 'Checked. The repeat is sent to this scale.'; cls = 'ok'; }
        else if (read) { line = 'Checked. Nothing is different.'; cls = 'ok'; }
        else { line = 'Not checked. The send will check it first.'; cls = 'warn'; }
        holder.appendChild(box(cls, name, line));
      });
      pv.drift.forEach(function (dr, i) {
        var plain = dr.plain || null;   /* staff words from the server for a drift nobody may override */
        holder.appendChild(box('bad', dr.scale, plain ? plain.plain : dr.message, (plain && plain.note) || dr.plain_note || null));
        if (dr.can_ack) {
          var key = 'd:' + dr.ack_token; needed.push(key);
          var words = (plain && plain.tick) || (draft.settle ? 'Yes. Settle it this way.' : 'I know this scale is different. Change it anyway.');
          holder.appendChild(checkbox('dr' + i, words, function (ev) { acked[key] = ev.target.checked; update(); }));
          if (acked[key]) { holder.lastChild.querySelector('input').checked = true; }
        } else {
          blocked = true;
          holder.appendChild(box('bad', 'Do not send',
            plain ? plain.blocked : (dr.different_product ? 'This scale holds a different product at this number. It cannot be forced from this page.' : 'This cannot be sent while the scale is like this.'),
            plain ? plain.more : 'Call Bart.'));
        }
      });
      if (frozenSeen) {
        /* Sending reads that scale again, which can freeze it for good. Nothing goes out until a person has looked. */
        blocked = true;
        holder.appendChild(box('bad', 'Do not send', 'A scale did not answer, and sending would read it again.', 'Tell Bart. Do not try again and again.'));
      }
      if (checkError) {
        /* The read check did not run. A change that needs empty slots is refused unless a review showed them. */
        blocked = true;
        holder.appendChild(errorBox(checkError));
        holder.appendChild(box('bad', 'Do not send', 'The scales could not be read for this check, so it cannot be sent yet.'));
        holder.appendChild(h('button', { class: 'btn', type: 'button', text: 'Check again', onclick: start }));
      }

      var onlyDelete = pv.differences.length > 0 && pv.differences.every(function (d) { return d.action === 'delete'; });
      var sendLabel = onlyDelete ? 'Delete from the scales' : 'Send to the scales';
      var sendBtn = h('button', { class: 'btn send' + (onlyDelete ? ' danger' : ''), type: 'button', text: sendLabel });
      function ready() { return !checking && !blocked && !S.configNotice && needed.every(function (k) { return acked[k]; }); }
      function update() {
        sendBtn.disabled = !ready();
        sendBtn.textContent = checking ? 'Checking the scales' : ((blocked || S.configNotice) ? 'Cannot send' : sendLabel);
      }
      sendBtn.addEventListener('click', function () {
        if (!ready()) { return; }
        sendBtn.disabled = true; sendBtn.textContent = 'Sending';
        var tokens = pv.drift.filter(function (d) { return d.ack_token && acked['d:' + d.ack_token]; }).map(function (d) { return d.ack_token; });
        var warnOk = pv.warnings.filter(function (x) { return x.requires_ack; }).length > 0;
        withPin(function () { return api('POST', '/api/send', { preview_id: pv.preview_id, acknowledge_warnings: warnOk, acknowledge_drift: tokens }); })
          .then(function () {
            saveLast(pv.preview_id, draft.title); dropDraft(); formMemory = {};
            location.hash = '#/result/' + encodeURIComponent(pv.preview_id);
          }, function (e) {
            if (e.code === 'config_changed') { S.configNotice = e.message; }
            update();
            var again = (e.code === 'preview_expired' || e.code === 'preview_stale');
            holder.appendChild(errorBox(e));
            if (e.code === 'preview_already_applied') {
              holder.appendChild(link('#/result/' + encodeURIComponent(pv.preview_id), 'See what happened'));
            } else if (again) {
              holder.appendChild(h('button', { class: 'btn', type: 'button', text: 'Check again', onclick: start }));
            }
          });
      });
      holder.appendChild(h('div', { class: 'sticky' }, sendBtn, back));
      update();
    }

    function start() {
      checking = true; checkError = null; latest = null; acked = {}; paint();
      var firstCall = draft.settle
        ? function () { return api('POST', '/api/settle/preview', draft.settle); }
        : function () { return api('POST', '/api/preview', { changes: draft.changes, read: false }); };
      withPin(firstCall).then(function (first) {
        latest = first; paint();
        if (!first.ok || draft.settle) { checking = false; return null; }
        return api('POST', '/api/preview', { changes: draft.changes, read: true }).then(function (second) {
          latest = second;
        }, function (e) { checkError = e; });
      }, function (e) {
        checking = false; latest = null;
        if (my === routeToken) {
          clear(holder);
          holder.appendChild(errorBox(e.code === 'cancelled' ? { message: 'The PIN is needed to make changes.' } : e));
          holder.appendChild(link(draft.back || '#/', 'Go back', 'btn second'));
        }
        return 'stop';
      }).then(function (stopped) {
        if (stopped === 'stop') { return; }
        checking = false; paint();
      });
    }
    start();
  }

  /* result */

  /* What a finished PLU change or delete means now. Only work every scale confirmed is called done.
     A repeat sends only the target, and for a PLU change the PLU. It never sends a price or a name
     from an old result, because that could put an old value back. */
  function pluResult(d, pid) {
    var out = [];
    if (d.state !== 'finished') { return out; }
    /* Every scale line says changed and checked. A scale left out shows as not touched, so it counts against this. */
    var everyScale = (d.rows || []).length > 0 && d.rows.every(function (r) { return r.status === 'verified'; });
    (d.changes || []).forEach(function (c) {
      var pc = pluChange(c);
      if (c.action === 'delete') {
        var gone = (c.before || {}).plu || c.plu;
        if (everyScale || !gone) { return; }
        out.push(box('warn', 'Check this', 'The delete of PLU ' + gone + ' may not have finished on every scale.',
                     'A scale that is not marked changed and checked has not confirmed it. Until it does, assume PLU ' + gone +
                     ' still works on that scale. Look at the scale.'));
        out.push(h('button', { class: 'btn second', type: 'button', text: 'Repeat the delete', onclick: function () {
          startRepeat('Repeat the delete of ' + gone, { action: 'delete', target_plu: gone }, { kind: 'delete', plu: gone, old: [gone] },
                      '#/result/' + encodeURIComponent(pid));
        } }));
        return;
      }
      if (!pc) { return; }
      if (everyScale) {
        out.push(box('ok', 'Done', 'PLU ' + pc.before + ' is now PLU ' + pc.after + '.',
                     'Use ' + pc.after + ' from now on. ' + pc.before + ' no longer works on the scales.'));
        return;
      }
      out.push(box('warn', 'Check this', 'The PLU change from ' + pc.before + ' to ' + pc.after + ' may not have finished on every scale.',
                   'A scale marked changed and checked uses ' + pc.after + ', and ' + pc.before + ' no longer works on it. Any other scale may still use ' +
                   pc.before + ', and ' + pc.before + ' may still work there. Look at each scale. Do not give ' + pc.before + ' or ' + pc.after +
                   ' to another item until this is done.'));
      out.push(h('button', { class: 'btn second', type: 'button', text: 'Repeat the PLU change', onclick: function () {
        startRepeat('Repeat the change of ' + pc.before, { action: 'edit', target_plu: pc.before, plu: pc.after },
                    { kind: 'plu_change', plu: pc.after, old: [pc.before] }, '#/result/' + encodeURIComponent(pid));
      } }));
    });
    return out;
  }
  function renderResult(pid, my) {
    var holder = h('div');
    var net = h('div');
    show([h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })), h('h1', { text: 'Result' }), net, holder]);
    var done = false;

    function paint(d) {
      if (my !== routeToken) { return; }
      clear(holder); clear(net);
      var sev = d.severity || 'working';
      var head = box(sev, TAG[sev] || '', d.heading || '');
      if (d.state === 'running' || d.state === 'not_sent') { head.firstChild.className = 'tag dots'; }
      if (d.state === 'stalled') { head.firstChild.textContent = 'Stopped'; }
      holder.appendChild(head);
      if (d.display && d.display !== d.heading && d.state === 'finished') { holder.appendChild(h('p', { class: 'plain', text: d.display })); }
      d.rows.forEach(function (r) {
        var b = h('div', { class: 'box ' + r.severity },
          h('span', { class: r.state === 'done' ? 'tag' : 'tag dots', text: TAG[r.severity] || '' }),
          h('div', { class: 'line', text: r.text }));
        if (r.now) { b.appendChild(h('div', { class: 'more', text: r.now })); }
        if (r.message && r.state === 'done') { b.appendChild(h('div', { class: 'more', text: r.message })); }
        if (r.may_be_frozen) { b.appendChild(h('div', { class: 'reboot', text: REBOOT })); }
        holder.appendChild(b);
      });
      if (d.state === 'running') {
        holder.appendChild(h('p', { class: 'plain', text: 'Sending one item to all three scales takes about two and a half minutes. Do not refresh the page and do not send again. You can lock your phone. Open this page again to see where it is.' }));
        if (d.may_have_stopped) {
          holder.appendChild(box('warn', 'Check this', 'If this stays here for more than five minutes, it may have stopped. Look at the scales.'));
        }
      }
      (d.notes || []).forEach(function (n) { holder.appendChild(box('warn', 'Check this', n, 'Open the item and use "Fix this difference" to settle it.')); });
      pluResult(d, pid).forEach(function (n) { holder.appendChild(n); });
      if (d.after_note) { holder.appendChild(box('warn', 'Next', d.after_note)); }
      if (d.state === 'error') { holder.appendChild(errorBox(d.error)); }
      if (d.state === 'stalled' && d.change_id) { holder.appendChild(resolveForm(d.change_id, function () { done = false; poll(); })); }
      if (d.changes && d.changes.length) {
        holder.appendChild(h('h2', { text: 'What was sent' }));
        d.changes.forEach(function (c) { holder.appendChild(diffBlock(c, true)); });
      }
      if (d.state !== 'running' && d.state !== 'stalled') { holder.appendChild(link('#/', 'Find another item')); }
    }

    function poll() {
      if (my !== routeToken || done) { return; }
      api('GET', '/api/send/' + encodeURIComponent(pid)).then(function (d) {
        paint(d);
        if (d.state === 'running') { timer = setTimeout(poll, 2000); }
        else if (d.state === 'not_sent') { done = true; }
        else { done = true; }
      }, function (e) {
        if (my !== routeToken) { return; }
        if (e.status === 404) { clear(holder); holder.appendChild(errorBox({ message: 'This change was not found.' })); done = true; return; }
        clear(net);
        net.appendChild(box('warn', 'Check this', 'Lost touch with the page. Trying again.', e.message));
        timer = setTimeout(poll, 3000);
      });
    }
    kick = function () { if (!done) { clearTimeout(timer); poll(); } };
    poll();
  }

  /* recent changes */

  function renderRecent(my) {
    return api('GET', '/api/changes').then(function (d) {
      if (my !== routeToken) { return; }
      var nodes = [h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })), h('h1', { text: 'Recent changes' })];
      if (!d.changes.length) { nodes.push(h('p', { class: 'plain', text: 'No changes yet.' })); }
      d.changes.forEach(function (c) {
        var what = c.what.map(function (w) { return (w.action || '') + ' ' + (w.plu || ''); }).join(', ');
        nodes.push(h('a', { class: 'result', href: '#/result/' + encodeURIComponent(c.preview_id) },
          h('div', { class: 'plu', text: what }), h('div', { class: 'desc', text: c.heading }),
          h('div', { class: 'small', text: fmtTime(c.started_at) })));
      });
      show(nodes);
    }, function (e) { if (my === routeToken) { show([errorBox(e)]); } });
  }

  /* routes */

  function route() {
    stopTimer();
    routeToken += 1;
    var my = routeToken;
    var parts = (location.hash.replace(/^#/, '') || '/').split('/').filter(Boolean).map(function (x) {
      try { return decodeURIComponent(x); } catch (e) { return x; }
    });
    loadState().then(function () {
      if (my !== routeToken) { return null; }
      var page = parts[0] || '';
      if (page === 'item' && parts[1]) { return renderItem(parts[1], my); }
      if (page === 'edit' && parts[1]) { return renderEdit(parts[1], my); }
      if (page === 'add') { return renderAdd(); }
      if (page === 'review') { return renderReview(my); }
      if (page === 'result' && parts[1]) { return renderResult(parts[1], my); }
      if (page === 'recent') { return renderRecent(my); }
      if (page === 'settle' && parts[1] && parts[2]) { return renderSettle(parts[1], parts[2], my); }
      if (page === 'orphans') { return renderOrphans(); }
      return renderHome(my);
    }).catch(function (e) { if (my === routeToken) { show([errorBox(e)]); } });
  }

  document.addEventListener('visibilitychange', function () { if (!document.hidden && kick) { kick(); } });
  window.addEventListener('online', function () { if (kick) { kick(); } });
  window.addEventListener('hashchange', route);
  route();
})();
