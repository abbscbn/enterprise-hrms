import {
  configureStore,
  createSlice,
  type PayloadAction,
} from "@reduxjs/toolkit";
import { api } from "./api";
const ui = createSlice({
  name: "ui",
  initialState: { message: "" },
  reducers: {
    notify: (state, action: PayloadAction<string>) => {
      state.message = action.payload;
    },
    dismiss: (state) => {
      state.message = "";
    },
  },
});
export const { notify, dismiss } = ui.actions;
export const makeStore = () =>
  configureStore({
    reducer: { [api.reducerPath]: api.reducer, ui: ui.reducer },
    middleware: (getDefault) => getDefault().concat(api.middleware),
  });
export type RootState = ReturnType<ReturnType<typeof makeStore>["getState"]>;
