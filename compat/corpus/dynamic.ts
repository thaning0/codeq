export function dispatchEvent(event: string): string {
  return event.toUpperCase();
}

function invoke(callback: (event: string) => string): string {
  return callback("created");
}

const callbacks = { created: dispatchEvent };
const alias = dispatchEvent;
const callbackResult = invoke(dispatchEvent);
const directResult = dispatchEvent("updated");

export { alias, callbackResult, callbacks, directResult };
