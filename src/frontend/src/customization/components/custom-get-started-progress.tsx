import { GetStartedProgress } from "@/components/core/folderSidebarComponent/components/sideBarFolderButtons/components/get-started-progress";
export function CustomGetStartedProgress({
  handleDismissDialog,
}: {
  handleDismissDialog: () => void;
}) {
  return <GetStartedProgress handleDismissDialog={handleDismissDialog} />;
}

export default CustomGetStartedProgress;
