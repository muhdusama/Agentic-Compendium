/* Render a safe, useful Markdown subset into real rich DOM, then optionally RTF.
 * Input is untrusted Hermes output. No raw HTML or executable URLs are used.
 */
(() => {
  'use strict';
  const el = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const tokens = /(!?\[[^\]]+\]\([^)\n]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|`[^`\n]+`|\*[^*\n]+\*|_[^_\n]+_)/g;

  function inline(text, parent, depth = 0) {
    if (depth > 4) { parent.append(document.createTextNode(text)); return; }
    let last = 0;
    for (const match of text.matchAll(tokens)) {
      const start = match.index;
      if (start > last) parent.append(document.createTextNode(text.slice(last, start)));
      const raw = match[0];
      const isImage = raw.startsWith('![');
      if (raw.startsWith('[') || isImage) {
        const parts = raw.match(/^!?\[([^\]]+)\]\(([^\s)]+)(?:\s+"[^"]*")?\)$/);
        let url;
        try { url = parts ? new URL(parts[2], location.href) : null; } catch (_) { url = null; }
        if (url && ['http:', 'https:', 'mailto:'].includes(url.protocol)) {
          const link = el('a', (isImage ? 'Image: ' : '') + parts[1]);
          link.href = url.href;
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          parent.append(link);
        } else parent.append(document.createTextNode(raw));
      } else {
        const paired = raw.startsWith('**') || raw.startsWith('__') || raw.startsWith('~~') ? 2 : 1;
        const tag = raw.startsWith('**') || raw.startsWith('__') ? 'strong'
          : raw.startsWith('~~') ? 'del' : raw.startsWith('`') ? 'code' : 'em';
        const node = el(tag);
        if (tag === 'code') node.textContent = raw.slice(1, -1);
        else inline(raw.slice(paired, -paired), node, depth + 1);
        parent.append(node);
      }
      last = start + raw.length;
    }
    if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
  }

  function cells(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/)
      .map(cell => cell.trim().replace(/\\\|/g, '|'));
  }
  const tableDivider = line => /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
  const bullet = line => line.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);

  function render(markdown) {
    const root = document.createDocumentFragment();
    const lines = String(markdown ?? '').replace(/\r\n?/g, '\n').split('\n');
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const fence = line.match(/^\s*(```+|~~~+)([^\n]*)$/);
      if (fence) {
        const content = [], marker = fence[1]; i++;
        while (i < lines.length && !lines[i].trimStart().startsWith(marker)) content.push(lines[i++]);
        if (i < lines.length) i++;
        const pre = el('pre'), code = el('code', content.join('\n'));
        if (fence[2].trim()) code.dataset.language = fence[2].trim().slice(0, 32);
        pre.append(code); root.append(pre); continue;
      }
      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        const h = el('h' + heading[1].length); inline(heading[2], h);
        root.append(h); i++; continue;
      }
      if (/^\s{0,3}(?:---+|___+|\*\*\*+)\s*$/.test(line)) {
        root.append(el('hr')); i++; continue;
      }
      if (i + 1 < lines.length && line.includes('|') && tableDivider(lines[i + 1])) {
        const table = el('table'), head = el('thead'), tr = el('tr');
        for (const cell of cells(line)) { const th = el('th'); inline(cell, th); tr.append(th); }
        head.append(tr); table.append(head); i += 2;
        const body = el('tbody');
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          const row = el('tr'), columns = cells(lines[i]);
          for (let n = 0; n < tr.children.length; n++) {
            const td = el('td'); inline(columns[n] || '', td); row.append(td);
          }
          body.append(row); i++;
        }
        table.append(body);
        const wrapper = el('div'); wrapper.className = 'rich-table-scroll';
        wrapper.append(table); root.append(wrapper); continue;
      }
      if (/^\s{0,3}>/.test(line)) {
        const quote = [], node = el('blockquote');
        while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
          quote.push(lines[i++].replace(/^\s{0,3}>\s?/, ''));
        }
        // Do not recursively interpret untrusted nested HTML as markup.
        for (const quoted of quote) { const p = el('p'); inline(quoted, p); node.append(p); }
        root.append(node); continue;
      }
      if (bullet(line)) {
        const ordered = /^\d/.test(bullet(line)[2]);
        const list = el(ordered ? 'ol' : 'ul');
        while (i < lines.length) {
          const entry = bullet(lines[i]);
          if (!entry || /^\d/.test(entry[2]) !== ordered) break;
          const li = el('li'), task = entry[3].match(/^\[([xX ])\]\s*(.*)$/);
          if (task) {
            const box = el('input'); box.type = 'checkbox'; box.disabled = true;
            box.checked = task[1].toLowerCase() === 'x'; box.setAttribute('aria-label', box.checked ? 'Completed' : 'Not completed');
            li.append(box); inline(task[2], li);
          } else inline(entry[3], li);
          list.append(li); i++;
        }
        root.append(list); continue;
      }
      const paragraph = [];
      while (i < lines.length && lines[i].trim() &&
             (!paragraph.length || (!/^\s{0,3}(?:#|>|```|~~~)/.test(lines[i]) && !bullet(lines[i])))) {
        paragraph.push(lines[i++]);
      }
      if (!paragraph.length) { i++; continue; }
      const p = el('p');
      for (let n = 0; n < paragraph.length; n++) {
        if (n) p.append(el('br'));
        inline(paragraph[n], p);
      }
      root.append(p);
    }
    return root;
  }

  function toRTF(root) {
    function escape(text) {
      let output = '';
      for (let i = 0; i < text.length; i++) {
        const c = text.charCodeAt(i), char = text[i];
        if (char === '\\' || char === '{' || char === '}') output += '\\' + char;
        else if (char === '\n') output += '\\line ';
        else if (c > 127) output += '\\u' + (c > 32767 ? c - 65536 : c) + '?';
        else output += char;
      }
      return output;
    }
    function walk(node) {
      if (node.nodeType === Node.TEXT_NODE) return escape(node.nodeValue || '');
      if (![Node.ELEMENT_NODE, Node.DOCUMENT_FRAGMENT_NODE].includes(node.nodeType)) return '';
      const tag = node.nodeName.toLowerCase();
      const children = Array.from(node.childNodes).map(walk).join('');
      if (/^h[1-6]$/.test(tag)) return `{\\b\\fs${tag === 'h1' ? 36 : tag === 'h2' ? 30 : 25} ${children}}\\par `;
      if (tag === 'strong' || tag === 'b') return `{\\b ${children}}`;
      if (tag === 'em' || tag === 'i') return `{\\i ${children}}`;
      if (tag === 'a') return `{\\ul ${children}}`;
      if (tag === 'code' || tag === 'pre') return `{\\f1 ${children}}${tag === 'pre' ? '\\par ' : ''}`;
      if (tag === 'li') return `\\bullet\\tab ${children}\\par `;
      if (tag === 'td' || tag === 'th') return `${children}\\tab `;
      if (tag === 'tr') return `${children}\\par `;
      if (tag === 'br') return '\\line ';
      if (tag === 'hr') return '\\par ---\\par ';
      if (tag === 'p' || tag === 'blockquote') return `${children}\\par `;
      return children;
    }
    return `{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0\\fswiss Arial;}{\\f1\\fmodern Menlo;}}\\uc1\\fs22 ${walk(root)}}`;
  }

  window.CompendiumRichText = {render, toRTF};
})();
