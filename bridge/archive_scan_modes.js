'use strict';

const ARCHIVE_SCAN_MODE = Object.freeze({
  STARTUP: 'startup',
  PERIODIC_CACHE: 'periodic_cache',
});

function archiveScanAllowsPagination(mode) {
  return mode === ARCHIVE_SCAN_MODE.STARTUP;
}

module.exports = { ARCHIVE_SCAN_MODE, archiveScanAllowsPagination };
