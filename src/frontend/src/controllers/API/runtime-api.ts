import axios from "axios";

export const runtimeApi = axios.create({
  baseURL: "",
  withCredentials: true,
});
