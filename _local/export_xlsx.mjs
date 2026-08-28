import './network_off.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

function safe(value) {
  if (typeof value !== 'string') return value;
  const text = value.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, '');
  // Source text must never become an Excel formula, DDE command or link formula.
  return /^[\s]*[=+@-]/.test(text) ? "'" + text : text;
}

export async function buildReports(payload, output, previewDir) {
  for (const spec of payload.workbooks) {
    const workbook = Workbook.create();
    for (const definition of spec.sheets) workbook.worksheets.add(definition.name);
    for (const [index, definition] of spec.sheets.entries()) {
      const sheet = workbook.worksheets.getItem(definition.name);
      sheet.showGridLines = false;
      const matrix = [definition.headers, ...definition.rows].map(row => row.map(safe));
      const rows = matrix.length, cols = definition.headers.length;
      const used = sheet.getRangeByIndexes(0, 0, rows, cols);
      used.values = matrix;
      used.format.font.name = 'Calibri';
      used.format.font.size = 11;
      used.format.wrapText = true;
      used.format.verticalAlignment = 'top';
      used.format.rowHeight = 36;
      for (let col = 0; col < cols; col++) {
        sheet.getRangeByIndexes(0, col, rows, 1).format.columnWidth = definition.widths?.[col] ?? 32;
      }
      const header = sheet.getRangeByIndexes(0, 0, 1, cols);
      header.format.fill = '#213F56';
      header.format.font.color = '#FFFFFF';
      header.format.font.bold = true;
      header.format.rowHeight = 45;
      sheet.freezePanes.freezeRows(1);
      if (rows > 1) {
        sheet.tables.add(`A1:${String.fromCharCode(64 + cols)}${rows}`, true, `ReportTable${index + 1}`);
        for (let row = 1; row < rows; row++) {
          // Estimate wrapping; full source cells can exceed Excel's visible row limit.
          const lines = Math.max(...matrix[row].map((v, col) => String(v ?? '').split('\n')
            .reduce((n, line) => n + Math.max(1, Math.ceil(line.length / ((definition.widths?.[col] ?? 32) * .9))), 0)));
          sheet.getRangeByIndexes(row, 0, 1, cols).format.rowHeight = Math.min(409, Math.max(30, lines * 15 + 12));
        }
      }
      if (definition.distribution && rows > 1) {
        const last = payload.records.length + 1;
        for (let row = 2; row <= rows; row++) {
          sheet.getRange(`B${row}`).formulas = [[`=COUNTIF('Неявка'!$C$2:$C$${last},A${row})`]];
          sheet.getRange(`C${row}`).formulas = [[`=B${row}/COUNTA('Неявка'!$A$2:$A$${last})`]];
        }
        sheet.getRange(`B2:B${rows}`).setNumberFormat('0');
        sheet.getRange(`C2:C${rows}`).setNumberFormat('0.0%');
      }
      // Preview is only enabled by the synthetic QA harness, never by normal launch.
      if (previewDir) {
        await fs.mkdir(previewDir, { recursive: true });
        const lastCol = String.fromCharCode(64 + Math.min(cols, 4));
        const preview = await workbook.render({ sheetName: definition.name,
          range: `A1:${lastCol}${Math.min(rows, 4)}`, scale: 1, format: 'png' });
        await fs.writeFile(path.join(previewDir, `${spec.filename}-${index + 1}.png`), new Uint8Array(await preview.arrayBuffer()));
      }
    }
    await fs.mkdir(output, { recursive: true });
    const file = await SpreadsheetFile.exportXlsx(workbook);
    await file.save(path.join(output, spec.filename));
  }
}

if (process.argv[2]) {
  try {
    const payload = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
    await buildReports(payload, process.argv[3], process.argv[4]);
    console.log('XLSX_OK');
  } catch {
    // Some library exceptions include cell contents. Never forward them to logs.
    console.error('XLSX_BUILD_FAILED');
    process.exitCode = 1;
  }
}
