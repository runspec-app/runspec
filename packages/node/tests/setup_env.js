// The installed-code integrity check would snapshot every fixture project a
// test runs parse() in. Tests that exercise it switch it back on themselves.
process.env.RUNSPEC_INTEGRITY = 'off';
