import { Suspense, useEffect } from "react";
import { RouterProvider } from "react-router-dom";
import router from "@/app-router";
import { useDarkStore } from "./stores/darkStore";

export default function App() {
  const dark = useDarkStore((state) => state.dark);
  useEffect(() => {
    if (!dark) {
      document.getElementById("body")!.classList.remove("dark");
    } else {
      document.getElementById("body")!.classList.add("dark");
    }
  }, [dark]);
  return (
    <Suspense
      fallback={
        <div
          className="flex h-screen w-screen items-center justify-center bg-background"
          role="status"
        >
          Loading…
        </div>
      }
    >
      <RouterProvider router={router} />
    </Suspense>
  );
}
