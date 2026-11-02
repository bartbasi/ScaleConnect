'use strict';
/* ScaleConnect page. Everything shown is put on the page as text, never as HTML,
   so odd characters in a description cannot do anything. */
(function () {
  var app = document.getElementById('app');
  var S = { signedIn: false, csrf: null, caps: { add: true, edit: true, delete: false, notes: {} }, scales: [],
            descMax: 35, pluEdit: false, busy: false, deleteNote: 'Deleting items is not available yet.' };
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
  function show(nodes) { clear(app); addKids(app, nodes); window.scrollTo(0, 0); }
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
      S.deleteNote = d.delete_note;
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

  function diffBlock(d) {
    var title; var cls = 'box'; var lines = [];
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
      title = 'Delete this item'; cls = 'box bad';
      lines.push(fieldLine('plu', null, d.plu || item.plu));
      lines.push(h('div', { class: 'field-row', text: (d.before || {}).description || '' }));
    } else {
      title = 'Change item ' + (d.plu || item.plu || '') + ' ' + ((d.before || {}).description || '');
      Object.keys(d.fields || {}).forEach(function (k) {
        if (k === 'timestamp') { return; }
        lines.push(fieldLine(k, d.fields[k].before, d.fields[k].after));
      });
    }
    return h('div', { class: cls }, h('div', { class: 'line', text: title }), lines);
  }

  function checkbox(id, text, onchange) {
    return h('label', { class: 'check', for: id },
      h('input', { type: 'checkbox', id: id, onchange: onchange }), h('span', { text: text }));
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
    var nodes = [h('h1', { text: 'Find an item' }), input, results];
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
  }

  /* one item */

  function renderItem(plu, my) {
    return api('GET', '/api/item/' + encodeURIComponent(plu)).then(function (d) {
      if (my !== routeToken) { return; }
      var it = d.item;
      var rows = d.scales.map(function (s) {
        var bad = s.flag === 'different' || s.flag === 'different_product' || s.flag === 'missing';
        var cls = s.flag === 'same' ? 'ok' : (bad ? 'bad' : (s.flag === 'unsure' ? 'warn' : ''));
        var tag = s.flag === 'same' ? 'Matches' : (s.flag === 'different_product' ? 'Different product' : (bad ? 'Different' : (s.flag === 'unsure' ? 'Not sure' : 'Unknown')));
        var more = s.last_read ? 'Last read: PLU ' + s.last_read.plu + ', ' + s.last_read.description + ', ' + money(s.last_read.price) +
          (s.last_read_at ? ' (' + fmtTime(s.last_read_at) + ')' : '') : null;
        var extra = s.can_change ? null : 'Changes are turned off for this scale.';
        var box1 = h('div', { class: 'box ' + cls }, h('span', { class: 'tag', text: tag }),
          h('div', { class: 'line', text: s.scale }), h('div', { class: 'more', text: s.text }),
          more ? h('div', { class: 'more', text: more }) : null, extra ? h('div', { class: 'more', text: extra }) : null);
        if (s.can_fix) {
          box1.appendChild(link('#/settle/' + encodeURIComponent(it.plu) + '/' + encodeURIComponent(s.scale), (s.flag === 'different_product' ? 'Bart only: settle this on ' : 'Fix this difference on ') + s.scale, 'btn second'));
        }
        return box1;
      });
      var nodes = [
        h('p', { class: 'small' }, h('a', { href: '#/', text: 'Back to search' })),
        h('div', { class: 'box' }, h('div', { class: 'big', text: it.plu }), h('div', { class: 'line', text: it.description }),
          h('div', { class: 'big', text: money(it.price) })),
        S.caps.edit ? link('#/edit/' + encodeURIComponent(it.plu), 'Change this item') : null,
        h('h2', { text: 'On the scales' }), rows,
        h('div', { id: 'checkmsg' }),
        h('button', { class: 'btn second', type: 'button', text: 'Check the scales now', onclick: function () {
          var m = document.getElementById('checkmsg'); clear(m);
          m.appendChild(box('working', 'Working', 'Reading the scales. This can take a minute.'));
          withPin(function () { return api('POST', '/api/check', { plu: it.plu }); }).then(function () { renderItem(plu, my); },
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
        nodes.push(h('button', { class: 'btn danger', type: 'button', text: 'Delete this item', onclick: function () {
          saveDraft({ title: 'Delete ' + it.plu, changes: [{ action: 'delete', target_plu: it.plu }], back: '#/item/' + encodeURIComponent(it.plu) });
          location.hash = '#/review';
        } }));
      } else {
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
      var pluInput = h('input', { type: 'text', id: 'plu', inputmode: 'numeric', maxlength: '4', value: mem.plu !== undefined ? mem.plu : it.plu, readonly: S.pluEdit ? null : true });
      var msg = h('div');
      var nodes = [
        h('p', { class: 'small' }, h('a', { href: '#/item/' + encodeURIComponent(it.plu), text: 'Back to the item' })),
        h('h1', { text: 'Change item' }),
        h('label', { for: 'price' }, 'Price', h('span', { class: 'hint', text: 'For example 9.99' })), price,
        h('label', { for: 'desc' }, 'Description', h('span', { class: 'hint', text: 'Up to ' + S.descMax + ' letters or numbers' })), desc,
        h('label', { for: 'plu' }, 'PLU'), pluInput,
        S.pluEdit ? null : h('p', { class: 'small', text: 'The PLU cannot be changed here yet. Use ScaleLink for a PLU change.' }),
        msg,
        h('div', { class: 'sticky' }, h('button', { class: 'btn', type: 'button', text: 'Review the change', onclick: function () {
          clear(msg);
          formMemory['edit/' + it.plu] = { price: price.value, desc: desc.value, plu: pluInput.value };
          var change = { action: 'edit', target_plu: it.plu };
          var any = false;
          if (price.value.trim() !== it.price) { change.price = price.value; any = true; }
          if (desc.value.trim() !== it.description) { change.description = desc.value; any = true; }
          if (S.pluEdit && pluInput.value.trim() !== it.plu) { change.plu = pluInput.value; any = true; }
          if (!any) { msg.appendChild(box('warn', null, 'Nothing is different yet.', 'Change the price or the description first.')); return; }
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
      var back = link(draft.back || '#/', 'Go back and fix', 'btn second');
      if (!pv) { holder.appendChild(h('p', { class: 'plain dots', text: 'Getting ready' })); return; }
      if (!pv.ok) {
        holder.appendChild(box('bad', 'Problem', 'This cannot be sent yet.'));
        pv.errors.forEach(function (e) { holder.appendChild(box('bad', null, e.message)); });
        holder.appendChild(back); return;
      }
      pv.differences.forEach(function (d) { holder.appendChild(diffBlock(d)); });

      var blocked = false; var needed = [];
      pv.warnings.forEach(function (wn, i) {
        var key = 'w:' + wn.code + ':' + i;
        holder.appendChild(box(wn.requires_ack ? 'warn' : 'working', wn.requires_ack ? 'Read this' : 'Note', wn.message));
        if (wn.requires_ack) {
          needed.push(key);
          holder.appendChild(checkbox('ack' + i, 'I have read this. Send it anyway.', function (ev) { acked[key] = ev.target.checked; update(); }));
          if (acked[key]) { holder.lastChild.querySelector('input').checked = true; }
        }
      });
      holder.appendChild(h('h2', { text: 'The scales' }));
      pv.scales.forEach(function (name) {
        var read = pv.scales_read.indexOf(name) >= 0;
        var mine = pv.drift.filter(function (d) { return d.scale === name; });
        var line; var cls;
        if (checking) { line = 'Checking'; cls = 'working'; }
        else if (mine.length) { line = 'Different from our list'; cls = 'bad'; }
        else if (read) { line = 'Checked. Nothing is different.'; cls = 'ok'; }
        else { line = 'Not checked. The send will check it first.'; cls = 'warn'; }
        holder.appendChild(box(cls, name, line));
      });
      pv.drift.forEach(function (dr, i) {
        holder.appendChild(box('bad', dr.scale, dr.message));
        if (dr.can_ack) {
          var key = 'd:' + dr.ack_token; needed.push(key);
          var words = draft.settle ? 'Yes. Settle it this way.' : 'I know this scale is different. Change it anyway.';
          holder.appendChild(checkbox('dr' + i, words, function (ev) { acked[key] = ev.target.checked; update(); }));
          if (acked[key]) { holder.lastChild.querySelector('input').checked = true; }
        } else {
          blocked = true;
          holder.appendChild(box('bad', 'Do not send',
            dr.different_product ? 'This scale holds a different product at this number. It cannot be forced from this page.' : 'This cannot be sent while the scale is like this.',
            'Call Bart.'));
        }
      });
      if (checkError) { holder.appendChild(errorBox(checkError)); }

      var sendBtn = h('button', { class: 'btn send', type: 'button', text: 'Send to the scales' });
      function ready() { return !checking && !blocked && needed.every(function (k) { return acked[k]; }); }
      function update() {
        sendBtn.disabled = !ready();
        sendBtn.textContent = checking ? 'Checking the scales' : (blocked ? 'Cannot send' : 'Send to the scales');
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
        holder.appendChild(b);
      });
      if (d.state === 'running') {
        holder.appendChild(h('p', { class: 'plain', text: 'This takes a few minutes. You can lock your phone. Open this page again to see where it is.' }));
        if (d.may_have_stopped) {
          holder.appendChild(box('warn', 'Check this', 'If this stays here for more than five minutes, it may have stopped. Look at the scales.'));
        }
      }
      (d.notes || []).forEach(function (n) { holder.appendChild(box('warn', 'Check this', n, 'Open the item and use "Fix this difference" to settle it.')); });
      if (d.state === 'error') { holder.appendChild(errorBox(d.error)); }
      if (d.state === 'stalled' && d.change_id) { holder.appendChild(resolveForm(d.change_id, function () { done = false; poll(); })); }
      if (d.changes && d.changes.length) {
        holder.appendChild(h('h2', { text: 'What was sent' }));
        d.changes.forEach(function (c) { holder.appendChild(diffBlock(c)); });
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
