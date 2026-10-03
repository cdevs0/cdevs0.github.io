#include <windows.h>
/*
youare.c
Compile with:
gcc -o youare youare.c -mwindows
*/
void leftClickAt(int x, int y) {
  int screen_width = GetSystemMetrics(SM_CXSCREEN);
  int screen_height = GetSystemMetrics(SM_CYSCREEN);
  int absolute_x = (x * 65535) / screen_width;
  int absolute_y = (y * 65535) / screen_height;
  INPUT inputs[2] = {0};
  inputs[0].type = INPUT_MOUSE;
  inputs[0].mi.dx = absolute_x;
  inputs[0].mi.dy = absolute_y;
  inputs[0].mi.dwFlags =
      MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTDOWN;
  inputs[1].type = INPUT_MOUSE;
  inputs[1].mi.dx = absolute_x;
  inputs[1].mi.dy = absolute_y;
  inputs[1].mi.dwFlags =
      MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTUP;
  SendInput(2, inputs, sizeof(INPUT));
}
int main(void) {
  int center_x = GetSystemMetrics(SM_CXSCREEN) / 2;
  int center_y = GetSystemMetrics(SM_CYSCREEN) / 2;
  ShellExecuteA(NULL, "open", "chrome.exe",
                "https://cdevs0.github.io/youare.html --start-fullscreen", NULL,
                SW_SHOWNORMAL);
  Sleep(5000);
  leftClickAt(center_x, center_y);
  return 0;
}
