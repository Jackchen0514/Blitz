package main

import (
	"crypto/subtle"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	listenAddr  = "127.0.0.1:28262"
	usersDBPath = "/etc/hysteria/users_data.json"
)

type User struct {
	Password            string `json:"password"`
	MaxDownloadBytes    int64  `json:"max_download_bytes"`
	ExpirationDays      int    `json:"expiration_days"`
	AccountCreationDate string `json:"account_creation_date"`
	Blocked             bool   `json:"blocked"`
	UploadBytes         int64  `json:"upload_bytes"`
	DownloadBytes       int64  `json:"download_bytes"`
	UnlimitedUser       bool   `json:"unlimited_user"`
}

// usersFileMu guards reads against users_data.json being rewritten mid-read
// (database.py's writes are not atomic).
var usersFileMu sync.Mutex

func loadUser(username string) (User, bool) {
	usersFileMu.Lock()
	data, err := os.ReadFile(usersDBPath)
	usersFileMu.Unlock()
	if err != nil {
		return User{}, false
	}

	var allUsers map[string]User
	if err := json.Unmarshal(data, &allUsers); err != nil {
		return User{}, false
	}

	user, ok := allUsers[strings.ToLower(username)]
	return user, ok
}

type httpAuthRequest struct {
	Addr string `json:"addr"`
	Auth string `json:"auth"`
	Tx   uint64 `json:"tx"`
}

type httpAuthResponse struct {
	OK bool   `json:"ok"`
	ID string `json:"id"`
}

func authHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req httpAuthRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}

	username, password, ok := strings.Cut(req.Auth, ":")
	if !ok {
		json.NewEncoder(w).Encode(httpAuthResponse{OK: false})
		return
	}

	user, found := loadUser(username)
	if !found {
		json.NewEncoder(w).Encode(httpAuthResponse{OK: false})
		return
	}

	if user.Blocked {
		json.NewEncoder(w).Encode(httpAuthResponse{OK: false})
		return
	}

	if subtle.ConstantTimeCompare([]byte(user.Password), []byte(password)) != 1 {
		time.Sleep(5 * time.Second)
		json.NewEncoder(w).Encode(httpAuthResponse{OK: false})
		return
	}

	if user.UnlimitedUser {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(httpAuthResponse{OK: true, ID: strings.ToLower(username)})
		return
	}

	if user.ExpirationDays > 0 {
		creationDate, err := time.Parse("2006-01-02", user.AccountCreationDate)
		if err == nil && time.Now().After(creationDate.AddDate(0, 0, user.ExpirationDays)) {
			json.NewEncoder(w).Encode(httpAuthResponse{OK: false})
			return
		}
	}

	if user.MaxDownloadBytes > 0 && (user.DownloadBytes+user.UploadBytes) >= user.MaxDownloadBytes {
		json.NewEncoder(w).Encode(httpAuthResponse{OK: false})
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(httpAuthResponse{OK: true, ID: strings.ToLower(username)})
}

func main() {
	http.HandleFunc("/auth", authHandler)
	log.Printf("Auth server starting on %s", listenAddr)
	if err := http.ListenAndServe(listenAddr, nil); err != nil {
		log.Fatalf("Failed to start server: %v", err)
	}
}