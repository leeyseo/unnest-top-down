import type { ConnectionLineComponentProps } from "@xyflow/react";
import useFlowStore from "@/stores/flowStore";

const ConnectionLineComponent = ({
  fromX,
  fromY,
  toX,
  toY,
  connectionLineStyle = {},
}: ConnectionLineComponentProps): JSX.Element => {
  const handleDragging = useFlowStore((state) => state.handleDragging);
  const color = handleDragging?.color;
  const accentColor = color
    ? `hsl(var(--datatype-${color}))`
    : "hsl(var(--primary))";

  return (
    <g>
      <path
        fill="none"
        // ! Replace hash # colors here
        strokeWidth={1.5}
        className={`animated`}
        style={{
          stroke: handleDragging ? accentColor : "",
          ...connectionLineStyle,
        }}
        d={`M${fromX},${fromY} C ${fromX} ${toY} ${fromX} ${toY} ${toX},${toY}`}
      />
      <ellipse
        cx={toX}
        cy={toY}
        fill="hsl(var(--background))"
        rx={4}
        ry={5.5}
        stroke={accentColor}
        className=""
        strokeWidth={1.5}
      />
    </g>
  );
};

export default ConnectionLineComponent;
