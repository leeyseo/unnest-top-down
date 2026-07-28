export type GetCodeType = {
  flowId: string;
  flowName: string;
  isAuth?: boolean;
  webhookAuthEnable?: boolean;
  tweaksBuildedObject?: {};
  endpointName?: string | null;
  activeTweaks?: boolean;
  copy?: boolean;
};
