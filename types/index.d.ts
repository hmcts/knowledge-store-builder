declare module "claude-code" {
  interface PluginState {
    "knowledge-store": {
      /** Set once `knowledgestore merge-inputs` has run in this session. */
      mergeInputsRan?: boolean;
    };
  }
}
