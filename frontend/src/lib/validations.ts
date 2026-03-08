import { z } from "zod";

export const loginSchema = z.object({
  email: z.string().min(1, "required").email("invalidEmail"),
  password: z.string().min(1, "required"),
});

export type LoginFormInput = z.infer<typeof loginSchema>;

export const registerSchema = z.object({
  email: z.string().min(1, "required").email("invalidEmail"),
  password: z.string().min(1, "required").min(8, "minLength"),
});

export type RegisterFormInput = z.infer<typeof registerSchema>;
