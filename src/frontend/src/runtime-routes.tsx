import { createBrowserRouter, Navigate } from "react-router-dom";
import { BASENAME } from "./customization/config-constants";
import RuntimePage from "./pages/RuntimePage";

const runtimeRouter = createBrowserRouter(
  [
    {
      path: "/",
      element: <RuntimePage />,
    },
    {
      path: "*",
      element: <Navigate replace to="/" />,
    },
  ],
  { basename: BASENAME || undefined },
);

export default runtimeRouter;
