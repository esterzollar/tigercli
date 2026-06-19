declare module "marked-terminal" {
  import type {MarkedExtension} from "marked";

  export function markedTerminal(options?: Record<string, unknown>): MarkedExtension;
  const markedTerminalDefault: typeof markedTerminal;
  export default markedTerminalDefault;
}
