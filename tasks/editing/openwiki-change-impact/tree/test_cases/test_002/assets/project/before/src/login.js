import { profileId } from "./profile.js";
export const login = () => `session:${profileId()}`;
