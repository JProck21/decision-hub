-- Convert legacy eval_status strings to A/B/C/F grades
UPDATE versions SET eval_status = 'A' WHERE eval_status = 'passed';
UPDATE versions SET eval_status = 'C' WHERE eval_status = 'pending';
UPDATE versions SET eval_status = 'F' WHERE eval_status = 'failed';
