import { classify } from '../src/http';

describe('classify', () => {
  it.each([
    [200, 'ok'],
    [204, 'ok'],
    [301, 'redirect'],
    [302, 'redirect'],
    [404, 'client-error'],
    [403, 'client-error'],
    [500, 'server-error'],
    [503, 'server-error'],
    [100, 'unknown'],
    [0, 'unknown'],
  ])('classifies %i as %s', (status, expected) => {
    expect(classify(status as number)).toBe(expected);
  });
});
