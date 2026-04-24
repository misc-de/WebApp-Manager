# Manual Runtime Customization Tests

These pages are small local probes for checking whether WebApp Manager runtime CSS
and JavaScript customizations are actually applied inside managed browser profiles.

Serve the folder locally so the pages are reachable over `http://`:

```bash
python3 -m http.server 8000 -d tests/manual
```

Then use one of these addresses in a WebApp entry:

- `http://127.0.0.1:8000/css_runtime_check.html`
- `http://127.0.0.1:8000/js_runtime_check.html`

Suggested inline CSS for `css_runtime_check.html`:

```css
body {
  background: linear-gradient(135deg, #ffe08a, #ffc8dd) !important;
}

#css-marker {
  transform: scale(1.05) rotate(-1deg);
  border-color: #d62828 !important;
  box-shadow: 0 0 0 6px rgba(214, 40, 40, 0.18);
}

.probe-chip {
  background: #111827 !important;
  color: #f9fafb !important;
}
```

Suggested inline JavaScript for `js_runtime_check.html`:

```javascript
document.title = 'JS ACTIVE';
document.body.dataset.webappRuntime = 'ok';
document.getElementById('js-status').textContent = 'JavaScript customization active';
document.getElementById('js-status').style.background = '#14532d';
document.getElementById('js-status').style.color = '#f0fdf4';
document.getElementById('js-log').textContent =
  'runtime=' + document.body.dataset.webappRuntime + ' @ ' + new Date().toISOString();
```
