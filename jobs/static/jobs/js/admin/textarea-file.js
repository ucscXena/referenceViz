// "Load from file" buttons for admin textareas. Reading happens in the
// browser: the file only fills in the textarea, and nothing is saved until
// the form is.
document.addEventListener('change', function (e) {
  var input = e.target;
  if (!input.matches('input[data-load-into]')) return;
  var file = input.files[0];
  if (!file) return;
  var textarea = document.getElementById(input.dataset.loadInto);
  var status = input.closest('.textarea-file').querySelector('.textarea-file-status');
  file.text().then(function (text) {
    var note = 'Loaded ' + file.name + '. Save to keep it.';
    if (input.dataset.format === 'json') {
      try {
        text = JSON.stringify(JSON.parse(text), null, 2);
      } catch (err) {
        note = file.name + ' is not valid JSON (' + err.message + '). Loaded as is; fix it before saving.';
      }
    }
    textarea.value = text;
    status.textContent = note;
    input.value = '';  // so choosing the same file again reloads it
  });
});
