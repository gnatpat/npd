What I've done:

Mainly following the set-up guide here: https://www.digitalocean.com/community/tutorial_series/new-ubuntu-14-04-server-checklist

Created a new user, nathan
Enabled firewall (ufw)

Followed https://gist.github.com/Nilpo/8ed5e44be00d6cf21f22 to set up git (/site and /site.git)

/static is directly mapped to /static
/resources is used to build the site

nginx is configured in /etc/nginx - /etc/nginx/sites-available/natpat.net

Following https://www.digitalocean.com/community/tutorials/how-to-secure-nginx-with-let-s-encrypt-on-ubuntu-20-04 for https

For shogi - 
added systemctl config to /etc/systemd/system/shogi.service
allowed `sudo systemctl` calls without password by adding /etc/sudoers.d/site file
`sudo systemctl <start|status|stop|...> shogi`

For blog -
git cloned blog src. Build whl. Created /blog and venv inside. Installed blog whl into that venv. Created DB and user
with INSTANCE_PATH=/blog.
`sudo systemctl <start|status|stop|...> blog`

For pokemon -
git cloned pokemon src.
check update-pokemon and run-pokemon for more. Also created systemd config (/etc/systemd/system/pokemon.service)