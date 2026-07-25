import { type Connection, Handle, Position } from "@xyflow/react";
import { memo, useCallback, useMemo, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { useDarkStore } from "@/stores/darkStore";
import useFlowStore from "@/stores/flowStore";
import type { APIDataType } from "@/types/api";
import type { groupedObjType } from "@/types/components";
import type {
  NodeDataType,
  sourceHandleType,
  targetHandleType,
} from "@/types/flow";
import { nodeColorsName } from "@/utils/styleUtils";
import ShadTooltip from "../../../../components/common/shadTooltipComponent";
import {
  isValidConnection,
  scapedJSONStringfy,
} from "../../../../utils/reactflowUtils";
import { cn, groupByFamily } from "../../../../utils/utils";
import HandleTooltipComponent from "../HandleTooltipComponent";

const BASE_HANDLE_STYLES = {
  width: "32px",
  height: "32px",
  top: "50%",
  position: "absolute" as const,
  zIndex: 30,
  background: "transparent",
  border: "none",
} as const;

const HandleContent = memo(function HandleContent({
  isNullHandle,
  isMuted,
  handleColor,
  isHovered,
  openHandle,
  testIdComplement,
  title,
  showNode,
  left,
}: {
  isNullHandle: boolean;
  isMuted: boolean;
  handleColor: string;
  isHovered: boolean;
  openHandle: boolean;
  testIdComplement?: string;
  title: string;
  showNode: boolean;
  left: boolean;
}) {
  const contentStyle = useMemo(
    () => ({
      background: isNullHandle ? "hsl(var(--border))" : handleColor,
      width: isMuted && !isNullHandle ? "5px" : "8px",
      height: isMuted && !isNullHandle ? "8px" : "11px",
      borderRadius: "50% 50% 45% 45% / 58% 58% 42% 42%",
      transition: "box-shadow 150ms ease, opacity 150ms ease",
      opacity: isMuted && !isNullHandle ? 0 : 1,
      boxShadow:
        isMuted && !isNullHandle
          ? "none"
          : `0 0 0 2px hsl(var(--background)), 0 0 0 ${
              isHovered || openHandle ? "4px" : "3px"
            } ${isNullHandle ? "hsl(var(--border))" : handleColor}`,
      border: isNullHandle ? "2px solid hsl(var(--muted))" : "none",
    }),
    [isNullHandle, isMuted, handleColor, isHovered, openHandle],
  );

  return (
    <div
      data-testid={`div-handle-${testIdComplement}-${title.toLowerCase()}-${
        !showNode ? (left ? "target" : "source") : left ? "left" : "right"
      }`}
      className="noflow nowheel nopan noselect pointer-events-none absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 cursor-crosshair rounded-full"
      style={contentStyle}
    />
  );
});

const HandleRenderComponent = memo(function HandleRenderComponent({
  left,
  tooltipTitle = "",
  proxy,
  id,
  title,
  myData,
  colors,
  setFilterEdge,
  showNode,
  testIdComplement,
  nodeId,
  colorName,
}: {
  left: boolean;
  tooltipTitle?: string;
  proxy?: { field: string; id: string };
  id?: targetHandleType | sourceHandleType;
  title: string;
  myData: APIDataType;
  colors: string[];
  setFilterEdge: (newState: groupedObjType[]) => void;
  showNode: boolean;
  testIdComplement?: string;
  nodeId: string;
  colorName?: string[];
}) {
  const [isHovered, setIsHovered] = useState(false);
  const [openTooltip, setOpenTooltip] = useState(false);

  const idType = id && "type" in id ? id.type : undefined;

  const isLocked = useFlowStore(
    useShallow((state) => state.currentFlow?.locked),
  );

  const edges = useFlowStore((state) => state.edges);

  // Check if this node is in "connect other models" mode
  const isInConnectionMode = useFlowStore(
    useCallback(
      (state) => {
        if (idType !== "model" || !left) return false;
        const node = state.nodes.find((n) => n.id === nodeId);
        return (node?.data as NodeDataType)?._connectionMode === true;
      },
      [nodeId, idType, left],
    ),
  );

  const {
    setHandleDragging,
    setFilterType,
    setFilterComponent,
    handleDragging,
    filterType,
    onConnect,
  } = useFlowStore(
    useCallback(
      (state) => ({
        setHandleDragging: state.setHandleDragging,
        setFilterType: state.setFilterType,
        setFilterComponent: state.setFilterComponent,
        handleDragging: state.handleDragging,
        filterType: state.filterType,
        onConnect: state.onConnect,
      }),
      [],
    ),
  );

  const dark = useDarkStore((state) => state.dark);

  const myId = useMemo(
    () => scapedJSONStringfy(proxy ? { ...id, proxy } : (id ?? {})),
    [id, proxy],
  );

  const getConnection = (semiConnection: {
    source?: string;
    sourceHandle?: string;
    target?: string;
    targetHandle?: string;
  }) => ({
    source: semiConnection.source ?? nodeId,
    sourceHandle: semiConnection.sourceHandle ?? myId,
    target: semiConnection.target ?? nodeId,
    targetHandle: semiConnection.targetHandle ?? myId,
  });

  const {
    sameNode,
    ownHandle,
    openHandle,
    filterOpenHandle,
    filterPresent,
    currentFilter,
    isNullHandle,
    isMuted,
    handleColor,
  } = useMemo(() => {
    const sameDraggingNode =
      (!left ? handleDragging?.target : handleDragging?.source) === nodeId;
    const sameFilterNode =
      (!left ? filterType?.target : filterType?.source) === nodeId;

    const ownDraggingHandle =
      handleDragging &&
      (left ? handleDragging?.target : handleDragging?.source) &&
      (left ? handleDragging.targetHandle : handleDragging.sourceHandle) ===
        myId;

    const ownFilterHandle =
      filterType &&
      (left ? filterType?.target : filterType?.source) === nodeId &&
      (left ? filterType.targetHandle : filterType.sourceHandle) === myId;

    const draggingOpenHandle =
      handleDragging &&
      (left ? handleDragging.source : handleDragging.target) &&
      !ownDraggingHandle
        ? isValidConnection(getConnection(handleDragging))
        : false;

    const filterOpenHandle =
      filterType &&
      (left ? filterType.source : filterType.target) &&
      !ownFilterHandle
        ? isValidConnection(getConnection(filterType))
        : false;

    const openHandle = filterOpenHandle || draggingOpenHandle;
    const filterPresent = handleDragging || filterType;

    const connectedEdge = edges.find(
      (edge) => edge.target === nodeId && edge.targetHandle === myId,
    );
    const outputType = connectedEdge?.data?.sourceHandle?.output_types?.[0];
    const connectedColor = (outputType && nodeColorsName[outputType]) || "gray";

    // Model handles that initiated connection mode on this node should not be nulled
    const isOwnModelConnectionMode =
      idType === "model" && left && filterType?.target === nodeId;

    const isNullHandle =
      filterPresent &&
      !(
        openHandle ||
        ownDraggingHandle ||
        ownFilterHandle ||
        isOwnModelConnectionMode
      );

    // Create a Set from colorName to remove duplicates
    const colorNameSet = new Set(colorName || []);
    const uniqueColorCount = colorNameSet.size;
    const firstUniqueColor =
      colorName && colorName.length > 0 ? colorName[0] : "";

    const handleColorName = connectedEdge
      ? connectedColor
      : uniqueColorCount > 1
        ? "secondary-foreground"
        : "datatype-" + firstUniqueColor;

    const handleColor = isNullHandle
      ? dark
        ? "hsl(var(--accent-gray))"
        : "hsl(var(--accent-gray-foreground)"
      : connectedEdge
        ? "hsl(var(--datatype-" + connectedColor + "))"
        : uniqueColorCount > 1
          ? "hsl(var(--secondary-foreground))"
          : "hsl(var(--datatype-" + firstUniqueColor + "))";

    const currentFilter = left
      ? {
          targetHandle: myId,
          target: nodeId,
          source: undefined,
          sourceHandle: undefined,
          type: tooltipTitle,
          color: handleColorName,
        }
      : {
          sourceHandle: myId,
          source: nodeId,
          target: undefined,
          targetHandle: undefined,
          type: tooltipTitle,
          color: handleColorName,
        };

    const isModelType = idType === "model";
    const isMuted =
      isModelType && !connectedEdge && !filterPresent && !isInConnectionMode;

    return {
      sameNode: sameDraggingNode || sameFilterNode,
      ownHandle: ownDraggingHandle || ownFilterHandle,
      openHandle,
      filterOpenHandle,
      filterPresent,
      currentFilter,
      isNullHandle,
      isMuted,
      handleColor,
    };
  }, [
    left,
    handleDragging,
    filterType,
    nodeId,
    myId,
    dark,
    colors,
    colorName,
    tooltipTitle,
    edges,
    id,
    isInConnectionMode,
  ]);

  const handleMouseDown = useCallback(
    (event: React.MouseEvent) => {
      if (event.button === 0) {
        setHandleDragging(currentFilter);
        const handleMouseUp = () => {
          setHandleDragging(undefined);
          document.removeEventListener("mouseup", handleMouseUp);
        };
        document.addEventListener("mouseup", handleMouseUp);
      }
    },
    [currentFilter, setHandleDragging],
  );

  const handleClick = useCallback(() => {
    const nodes = useFlowStore.getState().nodes;
    setFilterEdge(groupByFamily(myData, tooltipTitle!, left, nodes!));
    setFilterType(currentFilter);
    setFilterComponent("");
    if (filterOpenHandle && filterType) {
      onConnect(getConnection(filterType));
      setFilterType(undefined);
      setFilterEdge([]);
      setFilterComponent("");
    }
  }, [
    myData,
    tooltipTitle,
    left,
    setFilterEdge,
    setFilterType,
    setFilterComponent,
    currentFilter,
    filterOpenHandle,
    filterType,
    onConnect,
  ]);

  const handleMouseEnter = useCallback(() => setIsHovered(true), []);
  const handleMouseLeave = useCallback(() => setIsHovered(false), []);
  const handleMouseUp = useCallback(() => setOpenTooltip(false), []);
  const handleContextMenu = useCallback(
    (e: React.MouseEvent) => e.preventDefault(),
    [],
  );

  return (
    <div>
      <ShadTooltip
        open={openTooltip && !isLocked}
        setOpen={setOpenTooltip}
        styleClasses={cn("tooltip-fixed-width custom-scroll nowheel bottom-2")}
        delayDuration={1000}
        content={
          <HandleTooltipComponent
            isInput={left}
            tooltipTitle={tooltipTitle}
            isConnecting={!!filterPresent && !ownHandle}
            isCompatible={openHandle}
            isSameNode={sameNode && !ownHandle}
            left={left}
          />
        }
        side={left ? "left" : "right"}
      >
        <Handle
          type={left ? "target" : "source"}
          position={left ? Position.Left : Position.Right}
          id={myId}
          isValidConnection={(connection) =>
            isLocked ? false : isValidConnection(connection as Connection)
          }
          className={cn(
            `group/handle z-50 transition-all`,
            !showNode && "no-show",
          )}
          style={{
            ...BASE_HANDLE_STYLES,
            pointerEvents: isLocked ? "none" : "auto",
          }}
          onClick={handleClick}
          onMouseUp={handleMouseUp}
          onContextMenu={handleContextMenu}
          onMouseDown={handleMouseDown}
          onMouseEnter={handleMouseEnter}
          onMouseLeave={handleMouseLeave}
          data-testid={`handle-${testIdComplement}-${title.toLowerCase()}-${
            !showNode ? (left ? "target" : "source") : left ? "left" : "right"
          }`}
        >
          <HandleContent
            isNullHandle={isNullHandle ?? false}
            isMuted={isMuted ?? false}
            handleColor={handleColor}
            isHovered={isHovered}
            openHandle={openHandle}
            testIdComplement={testIdComplement}
            title={title}
            showNode={showNode}
            left={left}
          />
        </Handle>
      </ShadTooltip>
    </div>
  );
});

export default HandleRenderComponent;
